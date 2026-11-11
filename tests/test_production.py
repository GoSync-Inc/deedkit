"""Promotion guards use synthetic GitHub/Compose responses; no live runtime access."""
import ast
import contextlib
import copy
from datetime import datetime, timedelta, timezone
import fcntl
import fnmatch
import hashlib
import importlib.util
import json
import os
import py_compile
from pathlib import Path
import re
import resource
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import urllib.error

from hypothesis import given, strategies as st
import posixpath
import shutil
import pytest
import yaml

from scripts import production as p

SHA = "a" * 40
IMAGE = "sha256:" + "b" * 64
OLD_IMAGE = "sha256:" + "d" * 64
ROOT = Path(__file__).resolve().parents[1]
CHECK_SPACE, RETAIN_IMAGES, PROMOTABLE = p.check_space, p.retain_images, p.promotable


@pytest.fixture(autouse=True)
def no_host_state(tmp_path, monkeypatch):
    """No test reads this machine's production state, /etc/deedkit, containerd, tool or systemd directory."""
    for name in ("HOST_STATE", "CONFIG_DIRECTORY", "CONTAINERD_ROOT", "TOOL_ROOT", "SYSTEMD_DIRECTORY", "BACKUP_RUNNER",
                 "BACKUP_LOCK", "INFERENCE_WORKER_RECEIPT"):
        monkeypatch.setattr(p, name, tmp_path / "absent" / name.lower())


@pytest.fixture(autouse=True)
def no_apply_settle(monkeypatch):
    """apply's settle (ANYM-758) sleeps on the real clock; only the tests that set APPLY_SETTLE_SECONDS run it."""
    monkeypatch.setattr(p, "APPLY_SETTLE_SECONDS", 0, raising=False)


def comparison(sha, *, behind=0):
    """GitHub's `compare/main...<sha>` when main contains SHA (`behind` commits ahead of it), or diverged."""
    if sha != SHA:
        return {"status": "diverged", "ahead_by": 1, "behind_by": 1, "merge_base_commit": {"sha": "f" * 40}}
    return {"status": "behind" if behind else "identical", "ahead_by": 0, "behind_by": behind,
            "merge_base_commit": {"sha": SHA}}


def github(*, conclusion="success", event="push", branch="main", sha=SHA, jobs=None):
    run = {"id": 12, "run_attempt": 2, "head_sha": sha, "head_branch": branch,
           "event": event, "status": "completed", "conclusion": conclusion,
           "head_repository": {"full_name": p.REPOSITORY}, "html_url": "https://example.invalid/run/12"}
    required = [{"name": name, "status": "completed", "conclusion": "success"} for name in p.REQUIRED_JOBS]
    def get(path):
        if path.startswith("compare/main..."):
            return comparison(path.removeprefix("compare/main..."))
        if "/runs?" in path:
            return {"workflow_runs": [run]}
        assert "runs/12/attempts/2/jobs?" in path
        return {"jobs": required if jobs is None else jobs}
    return get, run, required


# spec: ANYM-626
def test_the_merge_queue_builds_six_entries_and_merges_one_all_green_squash_at_a_time():
    ruleset = json.loads((ROOT / "deploy/main-ruleset.json").read_text())
    queue = [rule["parameters"] for rule in ruleset["rules"] if rule["type"] == "merge_queue"]
    assert queue == [{"merge_method": "SQUASH", "grouping_strategy": "ALLGREEN", "max_entries_to_build": 6,
                      "min_entries_to_merge": 1, "max_entries_to_merge": 1, "min_entries_to_merge_wait_minutes": 0,
                      "check_response_timeout_minutes": 60}]
    assert "build concurrency 6, one merge at a time" in (ROOT / "docs/operations/parallel-development.md").read_text()


def test_exact_successful_main_push_and_all_required_jobs_allow_promotion():
    get, _, _ = github()
    assert p.verify(SHA, get)["run_attempt"] == 2


@pytest.mark.parametrize("candidate", ["main", "a" * 7, "A" * 40, "b" * 40, "refs/heads/feature", ""], ids=["branch-name", "short-sha", "uppercase", "outside-main", "feature-ref", "empty"])
def test_non_main_or_ambiguous_revision_cannot_reach_production(candidate):
    get, _, _ = github()
    with pytest.raises(p.Refused):
        p.verify(candidate, get)


@pytest.mark.parametrize("field,value", [("conclusion", "failure"), ("conclusion", "cancelled"),
    ("conclusion", "skipped"), ("status", "in_progress"), ("event", "workflow_dispatch"),
    ("event", "pull_request"), ("head_branch", "feature"), ("head_sha", "b" * 40),
    ("head_repository", {"full_name": "other/repo"})])
def test_failed_pending_or_unrelated_ci_never_authorizes_promotion(field, value):
    get, run, _ = github()
    run[field] = value
    with pytest.raises(p.Refused):
        p.verify(SHA, get)


def test_newer_failed_run_supersedes_an_old_green_run():
    get, latest, _ = github(conclusion="failure")
    old = {**latest, "id": 11, "conclusion": "success"}
    def response(path):
        return {"workflow_runs": [old, latest]} if "/runs?" in path else get(path)
    with pytest.raises(p.Refused):
        p.verify(SHA, response)


@pytest.mark.parametrize("outcome", ["failure", "skipped", "cancelled", None, "missing", "duplicate"])
def test_required_native_check_cannot_be_skipped_or_hidden_by_a_green_workflow(outcome):
    get, _, jobs = github()
    native = next(job for job in jobs if job["name"] == "native / synthetic")
    if outcome == "missing":
        jobs.remove(native)
    elif outcome == "duplicate":
        jobs.append(copy.deepcopy(native))
    else:
        native["conclusion"] = outcome
    with pytest.raises(p.Refused, match="required job"):
        p.verify(SHA, get)


def test_no_ci_run_fails_closed():
    get, _, _ = github()
    def response(path):
        return {"workflow_runs": []} if "/runs?" in path else get(path)
    with pytest.raises(p.Refused, match="no CI"):
        p.verify(SHA, response)


MANIFEST = "sha256:" + "9" * 64


def verified_image(target, config_digest, manifest=MANIFEST):
    """What the attestation verifier reports for a CI-published image of SHA (a fake verifier, no network)."""
    return {"reference": f"{p.REGISTRY}{target}@{manifest}", "digest": manifest, "config_digest": config_digest}


def fixture(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    project = {"name": "synthetic", "directory": str(tmp_path), "files": [str(tmp_path / "original.json")],
               "services": {"api": "runtime"}}
    settings = {"version": 1, "state_directory": str(state), "projects": [project]}
    document = {"name": "synthetic", "services": {"api": {"image": "old", "build": {"context": "/unused"},
        "environment": {"SYNTHETIC": "literal$password"}, "volumes": [{"source": "/existing", "target": "/data"}]},
        "db": {"image": "postgres:pinned"}}}
    (tmp_path / "original.json").write_text(json.dumps(document))
    calls, tagged = [], set()
    def command(argv, **kwargs):
        calls.append(argv)
        if "FETCH_HEAD" in argv:
            return SHA
        if argv[:2] == ["docker", "tag"]:
            tagged.add(argv[-1])
        if argv[:3] == ["docker", "image", "ls"]:
            return IMAGE if argv[-1] in tagged else ""
        if argv[:3] == ["docker", "image", "inspect"]:
            if "--format" in argv:
                return IMAGE
            # The CI-published runtime image carries the labels production.py gives its own builds.
            return json.dumps([{"Id": IMAGE, "Config": {"Labels": ImageStore.image("b")["Config"]["Labels"],
                                                        "Env": ["PATH=/usr/local/bin", "DEEDKIT_SOURCE_SHA=" + SHA]}}])
        if argv[:2] == ["docker", "compose"] and "config" in argv:
            value = json.loads(Path(argv[argv.index("-f") + 1]).read_text())
            if "--profile" not in argv:
                value["services"] = {key: unit for key, unit in value["services"].items() if not unit.get("profiles")}
            # Compose unescapes $$ without consulting a real environment.
            return json.dumps(value).replace("$$", "$")
        if argv[:2] == ["docker", "ps"]:
            # After apply started the replacement, the service runs on it.
            return "replacement123\n" if any("up" in call for call in calls) else "existing123\n"
        if argv[:2] == ["docker", "inspect"]:
            return json.dumps([{"Image": OLD_IMAGE if argv[2] == "existing123" else IMAGE, "State": {"Running": True}}])
        if "ps" in argv and "-q" in argv:
            return "replacement123\n"
        return ""
    monkeypatch.setattr(p, "run", command)
    monkeypatch.setattr(p, "promotable", lambda sha: {"sha": sha, "run_id": 12})
    monkeypatch.setattr(p, "published", lambda target, sha: verified_image(target, IMAGE))
    monkeypatch.setattr(p, "IMAGE_LOCK", tmp_path / "absent" / "image-build.lock")
    monkeypatch.setattr(p, "check_space", lambda directory: None)
    original = p.run
    monkeypatch.setattr(p, "run", lambda argv, **kw: str(tmp_path) if argv[:2] == ["docker", "info"] else original(argv, **kw))
    return settings, calls, document


def test_preparation_builds_only_exact_main_source_and_does_not_restart_services(tmp_path, monkeypatch):
    settings, calls, before = fixture(tmp_path, monkeypatch)
    receipt = p.prepare(settings, SHA)
    after = p.read(receipt["projects"][0]["file"])
    assert after["services"]["api"]["image"] == IMAGE
    assert after["services"]["api"]["volumes"] == before["services"]["api"]["volumes"]
    assert after["services"]["api"]["environment"]["SYNTHETIC"] == "literal$$password"
    assert after["services"]["db"] == before["services"]["db"]
    assert "build" not in after["services"]["api"]
    previous = p.read(Path(settings["state_directory"]) / SHA / "synthetic.before.json")
    assert previous["services"]["api"]["image"] == OLD_IMAGE
    assert "build" not in previous["services"]["api"]
    assert receipt["projects"][0]["running"]["api"] == {"container": "existing123", "image": OLD_IMAGE,
                                                        "settings": p.container_settings({})}
    # The verified commit itself is fetched, not whatever main's head is by now.
    assert ["git", "-C", str(Path(settings["state_directory"]) / SHA / "source"), "fetch", "--depth=1", "origin", SHA] in calls
    assert not any("stop" in call or "up" in call for call in calls)
    smoke = next(call for call in calls if call[:2] == ["docker", "run"])
    assert smoke[smoke.index("--network") + 1] == "none"
    assert not any("--volume" in call or "-v" in call for call in calls)


def test_private_preparation_checks_out_source_readable_by_image_users(tmp_path, monkeypatch):
    real_run = p.run
    upstream = tmp_path / "public-upstream"
    real_run(["git", "init", "--quiet", "--initial-branch=main", str(upstream)])
    (upstream / "package").mkdir()
    (upstream / "package" / "module.py").write_text("VALUE = 'synthetic'\n")
    (upstream / "entrypoint").write_text("#!/bin/sh\nexit 0\n")
    (upstream / "entrypoint").chmod(0o755)
    real_run(["git", "-C", str(upstream), "add", "."])
    real_run(["git", "-C", str(upstream), "-c", "user.name=Synthetic", "-c",
              "user.email=synthetic@example.invalid", "commit", "--quiet", "-m", "fixture"])
    sha = real_run(["git", "-C", str(upstream), "rev-parse", "HEAD"]).strip()
    settings, calls, _ = fixture(tmp_path, monkeypatch)
    simulated_runtime = p.run

    def command(argv, **kwargs):
        if argv[0] == "git":
            if "remote" in argv:
                argv = [*argv[:-1], str(upstream)]
            return real_run(argv, **kwargs)
        return simulated_runtime(argv, **kwargs)

    def inspect_build_source(source, revision, target, **kwargs):
        assert revision == sha
        # Docker COPY preserves these modes; root-owned 0600/0700 code is
        # unreadable to the production image's UID 10001.
        assert stat.S_IMODE((source / "package" / "module.py").stat().st_mode) == 0o644
        assert stat.S_IMODE((source / "package").stat().st_mode) == 0o755
        assert stat.S_IMODE((source / "entrypoint").stat().st_mode) == 0o755
        return IMAGE

    monkeypatch.setattr(p, "run", command)
    monkeypatch.setattr(p, "build_image", inspect_build_source)
    previous = os.umask(0o077)
    try:
        receipt = p.prepare(settings, sha)
        state = Path(settings["state_directory"])
        assert stat.S_IMODE(state.stat().st_mode) == 0o700
        assert stat.S_IMODE((state / sha).stat().st_mode) == 0o700
        assert stat.S_IMODE((state / sha / "prepared.json").stat().st_mode) == 0o600
        assert stat.S_IMODE(Path(receipt["projects"][0]["file"]).stat().st_mode) == 0o600
        assert stat.S_IMODE((state / sha / "synthetic.before.json").stat().st_mode) == 0o600
        assert os.umask(0o077) == 0o077
    finally:
        os.umask(previous)
    assert not any("stop" in call or "up" in call for call in calls)


def test_profiled_live_services_remain_in_the_candidate_without_starting_other_profiles(tmp_path, monkeypatch):
    settings, calls, document = fixture(tmp_path, monkeypatch)
    document["services"]["api"]["profiles"] = ["collectors"]
    document["services"]["optional"] = {"profiles": ["other"], "image": "unrelated:pinned"}
    Path(settings["projects"][0]["files"][0]).write_text(json.dumps(document))
    receipt = p.prepare(settings, SHA)
    candidate = p.read(receipt["projects"][0]["file"])
    assert candidate["services"]["api"]["image"] == IMAGE
    assert candidate["services"]["optional"] == document["services"]["optional"]
    calls.clear()
    p.apply(settings, SHA)
    mutations = [call for call in calls if "stop" in call or "up" in call]
    assert len(mutations) == 2
    assert all(call[-1] == "api" and "optional" not in call and "db" not in call for call in mutations)
    assert "--no-deps" in mutations[-1]


def test_main_moving_during_source_fetch_prevents_any_build(tmp_path, monkeypatch):
    settings, calls, _ = fixture(tmp_path, monkeypatch)
    original = p.run
    monkeypatch.setattr(p, "run", lambda argv, **kw: "c" * 40 if "FETCH_HEAD" in argv else original(argv, **kw))
    with pytest.raises(p.Refused, match="not the verified candidate commit"):
        p.prepare(settings, SHA)
    assert not any(call[:2] == ["docker", "build"] for call in calls)


@pytest.mark.parametrize("mount", [
    {"type": "bind", "source": "/host/telegram_session_owner.py", "target": "/voice-bridge/telegram_session_owner.py"},
    {"type": "bind", "source": "/host/source", "target": "/app"},
    {"type": "volume", "source": "unchecked-code", "target": "/usr/local/lib/python3.13/site-packages"},
    {"type": "bind", "source": "/host/extension.py", "target": "/data/extension.py"},
    {"type": "bind", "source": "/host/source", "target": "/data/../app"},
])
def test_host_mounted_code_prevents_preparation_before_any_build_or_restart(tmp_path, monkeypatch, mount):
    settings, calls, document = fixture(tmp_path, monkeypatch)
    document["services"]["api"]["volumes"].append(mount)
    Path(settings["projects"][0]["files"][0]).write_text(json.dumps(document))
    verified = []
    monkeypatch.setattr(p, "published", lambda target, sha: verified.append(target) or verified_image(target, IMAGE))
    with pytest.raises(p.Refused, match="host-mounted code"):
        p.prepare(settings, SHA)
    assert not any(call[:2] == ["docker", "build"] or "stop" in call or "up" in call for call in calls)
    # Nothing is obtained either: no attestation lookup, pull or tag before the refusal.
    assert verified == [] and not any(call[:2] in (["docker", "pull"], ["docker", "tag"]) for call in calls)


@pytest.mark.parametrize("name", ["PYTHONPATH", "NODE_PATH", "LD_PRELOAD", "LD_LIBRARY_PATH"])
def test_external_import_path_cannot_bypass_the_verified_image(tmp_path, monkeypatch, name):
    settings, calls, document = fixture(tmp_path, monkeypatch)
    document["services"]["api"]["environment"][name] = "/data/unchecked"
    Path(settings["projects"][0]["files"][0]).write_text(json.dumps(document))
    with pytest.raises(p.Refused, match="import path"):
        p.prepare(settings, SHA)
    assert not any(call[:2] == ["docker", "build"] or "stop" in call or "up" in call for call in calls)


BROKER = {"type": "bind", "source": "/opt/legacy/bridge", "target": "/broker"}
BYPASSES = {
    # Synthetic reproductions from the 2026-10-15 audit, then close variants.
    "broker-script": ({"volumes": [BROKER], "command": ["python", "/broker/telegram_session_owner.py"]}, "host-mounted code"),
    "broker-relative-script": ({"volumes": [BROKER], "command": ["python", "../broker/owner"]}, "host-mounted code"),
    "broker-module": ({"volumes": [BROKER], "command": ["python", "-m", "broker.telegram_session_owner"]}, "host-mounted code"),
    "attached-module": ({"volumes": [BROKER], "command": ["python3", "-Imbroker.owner"]}, "host-mounted code"),
    "working-dir": ({"working_dir": "/data/patch", "command": ["python", "-m", "messaging_service", "serve"]}, "host-mounted code"),
    "pythonuserbase": ({"environment": {"PYTHONUSERBASE": "/data/u"}}, "import path"),
    "pythonhome": ({"environment": {"PYTHONHOME": "/data/python"}}, "import path"),
    "pythonstartup": ({"environment": {"PYTHONSTARTUP": "/data/startup.py"}}, "import path"),
    "pythonsafepath": ({"environment": {"PYTHONSAFEPATH": ""}}, "import path"),
    "pycache-prefix": ({"environment": {"PYTHONPYCACHEPREFIX": "/data/pyc"}}, "import path"),
    "empty-pythonpath": ({"environment": {"PYTHONPATH": ""}}, "import path"),
    "listed-pythonpath": ({"environment": ["PYTHONPATH=/data"]}, "import path"),
    "user-site-home": ({"environment": {"HOME": "/data/home"}}, "import path"),
    "path-prefix": ({"environment": {"PATH": "/broker:/usr/local/bin:/usr/bin"}}, "import path"),
    "node-options": ({"environment": {"NODE_OPTIONS": "--require /data/x.js"}}, "import path"),
    "ld-audit": ({"environment": {"LD_AUDIT": "/data/audit.so"}}, "import path"),
    "gconv": ({"environment": {"GCONV_PATH": "/data/gconv"}}, "import path"),
    "entrypoint-sh-c": ({"entrypoint": ["sh", "-c", "python - <<<'print(1)'"]}, "inline"),
    "entrypoint-string": ({"entrypoint": "/bin/bash -lc 'exec python app.py'"}, "inline"),
    "python-c": ({"command": ["python", "-W", "ignore", "-c", "import runpy"]}, "inline"),
    "python-stdin": ({"command": ["python", "-"]}, "inline"),
    "wrapped-shell": ({"command": ["nice", "-n", "5", "busybox", "sh", "-ec", "true"]}, "inline"),
    "env-assignment": ({"command": ["env", "PYTHONPATH=/srv/extension", "python", "-m", "messaging_service"]}, "import path"),
    "env-split": ({"command": ["env", "-S", "python app.py"]}, "inline"),
    "node-require": ({"command": ["node", "--require=/srv/hook.js", "app.js"]}, "inline"),
    "embedded-mount": ({"command": ["perl", "-e", "do '/broker/bridge.pl'"]}, "host-mounted code"),
    "option-value-mount": ({"command": ["deedkit-gateway", "--plugin=/broker/x"]}, "host-mounted code"),
    "healthcheck-shell": ({"healthcheck": {"test": ["CMD-SHELL", "true"]}}, "inline"),
    "healthcheck-script": ({"healthcheck": {"test": ["CMD", "python", "/broker/probe.py"]}}, "host-mounted code"),
    "healthcheck-inline-mount": ({"healthcheck": {"test": ["CMD", "python", "-c", "exec(open('/broker/x').read())"]}}, "host-mounted code"),
    "secret-site-packages": ({"secrets": [{"source": "bridge", "target": "/usr/local/lib/python3.13/site-packages/bridge.pth"}]}, "host-mounted code"),
    "config-default-root": ({"configs": ["bridge"]}, "host-mounted code"),
    "lifecycle-hook": ({"post_start": [{"command": ["true"], "user": "root"}]}, "lifecycle"),
    "privileged": ({"privileged": True}, "privileged"),
    "cap-add": ({"cap_add": ["SYS_PTRACE"]}, "privileged"),
    "devices": ({"devices": ["/dev/fuse:/dev/fuse"]}, "privileged"),
    "volumes-from": ({"volumes_from": ["legacy-bridge"]}, "privileged"),
    "seccomp-unconfined": ({"security_opt": ["no-new-privileges:true", "seccomp=unconfined"]}, "privileged"),
    "network-host": ({"network_mode": "host"}, "namespaces"),
    "pid-host": ({"pid": "host"}, "namespaces"),
    "ipc-host": ({"ipc": "host"}, "namespaces"),
    "userns-host": ({"userns_mode": "host"}, "namespaces"),
    "pid-shared": ({"pid": "container:legacy-bridge"}, "namespaces"),
}


@pytest.mark.parametrize("case", BYPASSES, ids=list(BYPASSES))
def test_runtime_bypasses_prevent_preparation_before_any_build_or_restart(tmp_path, monkeypatch, case):
    settings, calls, document = fixture(tmp_path, monkeypatch)
    change, message = BYPASSES[case]
    unit = document["services"]["api"]
    for key, value in change.items():
        if key == "volumes":
            unit["volumes"] = unit["volumes"] + value
        elif key == "environment" and isinstance(value, dict):
            unit["environment"] = {**unit["environment"], **value}
        else:
            unit[key] = value
    Path(settings["projects"][0]["files"][0]).write_text(json.dumps(document))
    with pytest.raises(p.Refused, match=message):
        p.prepare(settings, SHA)
    assert not any(call[:2] == ["docker", "build"] or "stop" in call or "up" in call for call in calls)


@pytest.mark.parametrize("unit", [
    {"command": ["--policy", "/run/deedkit/policy.json", "--public-url", "https://gateway.example.invalid/deedkit",
                 "--host", "0.0.0.0", "--port", "13240"],
     "volumes": [{"source": "/srv/policy.json", "target": "/run/deedkit/policy.json"},
                 {"source": "/srv/keys", "target": "/run/deedkit/keys"}]},
    {"command": ["python", "-m", "telegram_session_owner", "--config", "/run/secrets/telegram_config"],
     "volumes": [{"source": "/srv/broker", "target": "/broker"}, {"source": "/srv/session", "target": "/sessions"}]},
    {"command": ["python", "-X", "dev", "-W", "default", "-m", "messaging_service", "serve"]},
    {"command": ["family-calendar", "--home=/data", "service", "serve"], "working_dir": "/app"},
    {"environment": {"PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1", "HOME": "/tmp",
                     "PATH": "/app/.venv/bin:/usr/local/bin:/usr/bin:/bin", "PROBE_FILES": "/data/files"}},
    {"healthcheck": {"test": ["CMD", "python", "-c", "import urllib.request", "https://gateway.example.invalid"]}},
    {"healthcheck": {"test": ["NONE"]}},
    {"secrets": ["database_url", {"source": "config", "target": "/run/secrets/telegram_config"}]},
    {"security_opt": ["no-new-privileges:true", "seccomp=/srv/chromium-seccomp.json"], "cap_drop": ["ALL"],
     "network_mode": "bridge", "ipc": "private"},
], ids=["gateway", "broker-owner", "interpreter-flags", "data-option", "environment", "healthcheck",
        "healthcheck-disabled", "secrets", "sandbox"])
def test_reviewed_runtime_settings_remain_promotable(unit):
    p.validate_runtime(unit)


class ComposeLoader(yaml.SafeLoader):
    """Read Compose override tags without constructing arbitrary Python objects."""


ComposeLoader.add_constructor("!override", lambda loader, node: loader.construct_sequence(node)
                              if isinstance(node, yaml.SequenceNode) else loader.construct_mapping(node))


def resolved(value):
    # Approximate `compose config` with synthetic values; no Compose or Docker runs.
    if isinstance(value, str):
        return re.sub(r"\$\{[^}]*\}", "synthetic", value)
    if isinstance(value, list):
        return [resolved(item) for item in value]
    if isinstance(value, dict):
        return {key: resolved(item) for key, item in value.items()}
    return value


def test_every_repository_application_service_passes_the_runtime_check():
    checked = []
    for path in sorted((ROOT / "deploy").glob("compose*.yaml")):
        for name, unit in yaml.load(path.read_text(), Loader=ComposeLoader)["services"].items():
            if name in {"db", "calendar-db", "operator"}:
                continue  # Never promotion targets: refused by name before any runtime check.
            unit = resolved(unit)
            unit["volumes"] = [volume if isinstance(volume, dict)
                               else dict(zip(("source", "target"), volume.split(":")[:2] if ":" in volume else ("", volume)))
                               for volume in unit.get("volumes", [])]
            if path.name == "compose.calls-telegram.yaml":
                # Existing limitation: /run/telegram-broker is not an approved mount root.
                with pytest.raises(p.Refused, match="host-mounted code"):
                    p.validate_runtime(unit)
                continue
            p.validate_runtime(unit)
            checked.append(name)
    assert {"api", "gateway", "calendar-api", "calendar-worker", "calls", "telegram", "whatsapp1", "whatsapp2",
            "indexer", "enricher", "enricher-local", "subscriptions"} <= set(checked)


def test_ci_becoming_red_after_build_prevents_a_prepared_release(tmp_path, monkeypatch):
    settings, calls, _ = fixture(tmp_path, monkeypatch)
    attempts = []
    def gate(sha):
        attempts.append(sha)
        if len(attempts) == 2:
            raise p.Refused("latest CI failed")
        return {"sha": sha}
    monkeypatch.setattr(p, "promotable", gate)
    with pytest.raises(p.Refused):
        p.prepare(settings, SHA)
    assert not (Path(settings["state_directory"]) / SHA / "prepared.json").exists()
    assert not any("stop" in call or "up" in call for call in calls)


def test_apply_uses_prepared_image_without_build_pull_migration_or_database_restart(tmp_path, monkeypatch):
    settings, calls, _ = fixture(tmp_path, monkeypatch)
    p.prepare(settings, SHA)
    calls.clear()
    result = p.apply(settings, SHA)
    assert result["status"] == "applied"
    stop = next(i for i, call in enumerate(calls) if "stop" in call)
    up = next(i for i, call in enumerate(calls) if "up" in call)
    assert stop < up
    assert calls[stop][-1] == calls[up][-1] == "api"
    assert "--no-build" in calls[up] and "--no-deps" in calls[up]
    assert calls[up][calls[up].index("--pull") + 1] == "never"
    assert not any("build" in call or "migrate" in call or "db" in call for call in calls)
    assert p.read(Path(settings["state_directory"]) / "current.json")["sha"] == SHA


@pytest.mark.parametrize("change", ["config", "container", "snapshot", "image", "ci"])
def test_changed_candidate_or_live_runtime_is_refused_before_first_stop(tmp_path, monkeypatch, change):
    settings, calls, _ = fixture(tmp_path, monkeypatch)
    receipt = p.prepare(settings, SHA)
    original = p.run
    if change == "config":
        settings["new_setting"] = True
    elif change == "snapshot":
        Path(receipt["projects"][0]["file"]).write_text("{}")
    elif change == "ci":
        monkeypatch.setattr(p, "promotable", lambda sha: (_ for _ in ()).throw(p.Refused("red CI")))
    else:
        def changed(argv, **kw):
            if change == "container" and argv[:2] == ["docker", "ps"]:
                return "other123\n"
            if change == "image" and argv[:3] == ["docker", "image", "inspect"]:
                return json.dumps([{"Id": IMAGE, "Config": {"Labels": {"org.opencontainers.image.revision": "c" * 40}}}])
            return original(argv, **kw)
        monkeypatch.setattr(p, "run", changed)
    calls.clear()
    with pytest.raises(p.Refused):
        p.apply(settings, SHA)
    assert not any("stop" in call or "up" in call for call in calls)


def host_runner(tmp_path, monkeypatch, installed, release):
    """The host backup runner file with `installed` bytes, and the candidate commit's runner with `release` bytes."""
    path = tmp_path / "ops" / "backup_runner.py"
    path.parent.mkdir()
    path.write_bytes(installed)
    monkeypatch.setattr(p, "BACKUP_RUNNER", path)
    expected = hashlib.sha1(b"blob %d\0" % len(release) + release).hexdigest()
    original = p.run
    def command(argv, **kw):
        if argv[-2:] == ["rev-parse", f"{SHA}:{p.BACKUP_RUNNER_SOURCE}"]:
            return expected + "\n"
        return original(argv, **kw)
    monkeypatch.setattr(p, "run", command)
    return expected


# spec: ANYM-488
def test_apply_is_refused_before_any_stop_while_the_host_backup_runner_is_not_the_releases(tmp_path, monkeypatch):
    settings, calls, _ = fixture(tmp_path, monkeypatch)
    p.prepare(settings, SHA)
    installed = b"stale runner\n"
    expected = host_runner(tmp_path, monkeypatch, installed, b"release runner\n")
    calls.clear()
    with pytest.raises(p.Refused, match="backup runner") as refused:
        p.apply(settings, SHA)
    assert expected[:12] in str(refused.value) and p.blob(p.BACKUP_RUNNER)[:12] in str(refused.value)
    assert not any("stop" in call or "up" in call for call in calls)
    assert not (Path(settings["state_directory"]) / SHA / "attempted.json").exists()


# spec: ANYM-488
def test_apply_goes_ahead_when_the_host_backup_runner_is_the_releases(tmp_path, monkeypatch):
    settings, calls, _ = fixture(tmp_path, monkeypatch)
    p.prepare(settings, SHA)
    host_runner(tmp_path, monkeypatch, b"release runner\n", b"release runner\n")
    assert p.apply(settings, SHA)["status"] == "applied"


# spec: ANYM-488
@pytest.mark.parametrize("case", ["symlink", "directory", "unreadable", "no-source"])
def test_an_unverifiable_host_backup_runner_refuses_apply_before_any_stop(tmp_path, monkeypatch, case):
    settings, calls, _ = fixture(tmp_path, monkeypatch)
    p.prepare(settings, SHA)
    host_runner(tmp_path, monkeypatch, b"release runner\n", b"release runner\n")
    path = p.BACKUP_RUNNER
    if case == "symlink":
        (tmp_path / "ops" / "real.py").write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(tmp_path / "ops" / "real.py")
    elif case == "directory":
        path.unlink()
        path.mkdir()
    elif case == "unreadable":
        monkeypatch.setattr(p.os, "open", lambda *a, **k: (_ for _ in ()).throw(PermissionError("synthetic")))
    else:
        original = p.run
        def missing(argv, **kw):
            if argv[-2:] == ["rev-parse", f"{SHA}:{p.BACKUP_RUNNER_SOURCE}"]:
                raise p.Refused("git failed (exit 128)")
            return original(argv, **kw)
        monkeypatch.setattr(p, "run", missing)
    calls.clear()
    with pytest.raises(p.Refused):
        p.apply(settings, SHA)
    assert not any("stop" in call or "up" in call for call in calls)
    assert not (Path(settings["state_directory"]) / SHA / "attempted.json").exists()


def test_failed_apply_is_not_blindly_retried_or_marked_current(tmp_path, monkeypatch):
    settings, calls, _ = fixture(tmp_path, monkeypatch)
    p.prepare(settings, SHA)
    original = p.run
    def failing(argv, **kw):
        if "up" in argv:
            raise p.Refused("synthetic start failure")
        return original(argv, **kw)
    monkeypatch.setattr(p, "run", failing)
    with pytest.raises(p.Refused):
        p.apply(settings, SHA)
    assert not (Path(settings["state_directory"]) / "current.json").exists()
    calls.clear()
    with pytest.raises(p.Refused, match="already attempted"):
        p.apply(settings, SHA)
    assert not any("stop" in call or "up" in call for call in calls)


def test_host_lock_rejects_concurrent_promotion(tmp_path):
    state = tmp_path / "private"
    with p.lock(state):
        with pytest.raises(p.Refused, match="lock"):
            with p.lock(state):
                pytest.fail("two promoters acquired the same lock")


def test_ci_gate_fails_for_failed_cancelled_or_skipped_dependencies():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    gate = workflow["jobs"]["gate"]
    # The reuse decisions gate only the second step (ANYM-322, the queue's pull request reuse); this first step is unchanged.
    # ANYM-578: the exclusion inventory (`added_list`) is a verification job too.
    assert gate["needs"] == ["added_list", "reuse", "pr_reuse", "checks", "native", "images"] and gate["if"] == "${{ always() }}"
    assert workflow["jobs"]["native"]["uses"] == "./.github/workflows/whatsmeow.yml"
    script = gate["steps"][0]["run"]
    for added, checks, native, images in [("success", "success", "success", "success"),
                                         ("success", "failure", "success", "success"),
                                         ("success", "success", "skipped", "success"),
                                         ("success", "cancelled", "success", "success"),
                                         ("success", "success", "success", "failure"),
                                         ("failure", "success", "success", "success"),
                                         ("skipped", "success", "success", "success")]:
        result = subprocess.run(["bash", "-e", "-c", script], env={"ADDED_LIST_RESULT": added, "CHECKS_RESULT": checks,
                                                                   "NATIVE_RESULT": native, "IMAGES_RESULT": images},
                                capture_output=True)
        assert (result.returncode == 0) == (added == checks == native == images == "success")


def test_ci_gate_refuses_a_manual_baseline_of_another_revision():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    step = workflow["jobs"]["gate"]["steps"][0]
    assert step["env"]["SOURCE_REF"] == "${{ inputs.source_ref || '' }}"
    assert step["env"]["EXPECTED_SHA"] == "${{ github.sha }}"
    green = {"ADDED_LIST_RESULT": "success", "CHECKS_RESULT": "success", "NATIVE_RESULT": "success",
             "IMAGES_RESULT": "success", "EXPECTED_SHA": SHA}
    for source_ref, passes in [("", True), (SHA, True), ("b" * 40, False), ("main", False),
                               ("refs/tags/v1.2.3", False)]:
        result = subprocess.run(["bash", "-e", "-c", step["run"]], env={**green, "SOURCE_REF": source_ref},
                                capture_output=True)
        assert (result.returncode == 0) == passes, source_ref


def test_disk_pressure_prevents_source_fetch_and_build(tmp_path, monkeypatch):
    settings, calls, _ = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(p, "check_space", lambda directory: (_ for _ in ()).throw(p.Refused("insufficient disk")))
    with pytest.raises(p.Refused, match="disk"):
        p.prepare(settings, SHA)
    assert calls == []


def test_disk_floor_is_enforced_on_the_real_preflight_function(tmp_path, monkeypatch):
    monkeypatch.setattr(p.shutil, "disk_usage", lambda _: SimpleNamespace(free=p.MIN_FREE_BYTES - 1))
    with pytest.raises(p.Refused, match="15 GiB"):
        p.check_space(tmp_path)
    monkeypatch.setattr(p.shutil, "disk_usage", lambda _: SimpleNamespace(free=p.MIN_FREE_BYTES))
    p.check_space(tmp_path)


def test_config_rejects_a_database_as_an_application_target(tmp_path, monkeypatch):
    settings, _, _ = fixture(tmp_path, monkeypatch)
    settings["projects"][0]["services"] = {"db": "runtime"}
    filename = tmp_path / "production.json"
    filename.write_text(json.dumps(settings))
    filename.chmod(0o600)
    with pytest.raises(p.Refused, match="application"):
        p.config(filename)


MESSAGING_STAGES = ("runtime", "indexer", "enricher-local", "whatsapp-native", "backup")


def test_production_messaging_stages_end_with_the_source_revision():
    # Parse instructions after folding continuations; the variable comes last so earlier layers stay cached.
    lines = (ROOT / "services/messaging/Dockerfile").read_text().replace("\\\n", " ").splitlines()
    stages, current = {}, None
    for line in (value.strip() for value in lines):
        if not line or line.startswith("#"):
            continue
        if line.upper().startswith("FROM "):
            current = line.split()[-1] if " AS " in line.upper() else None
            stages[current] = []
        elif current:
            stages[current].append(line)
    for stage in MESSAGING_STAGES:
        assert stages[stage][-2:] == ["ARG DEEDKIT_SOURCE_SHA", "ENV DEEDKIT_SOURCE_SHA=$DEEDKIT_SOURCE_SHA"], stage
        assert sum("DEEDKIT_SOURCE_SHA" in line for line in stages[stage]) == 2, stage
    assert {target for target, build in p.BUILDS.items() if build[1] == "services/messaging/Dockerfile"} == set(MESSAGING_STAGES)


# spec: ANYM-662
@pytest.mark.parametrize("dockerfile", ["services/gateway/Dockerfile", "services/calendar/Dockerfile", "services/calls/Dockerfile"])
def test_gateway_calendar_and_calls_images_end_with_the_source_revision(dockerfile):
    # production.py already passes the build argument to every target; the final stage must keep it as ENV so the
    # service can report its build. It sits after the build layers so they stay cached; the calls image keeps CMD
    # last (test_coverage_deployment pins it).
    # reads: services/gateway/Dockerfile
    # reads: services/calendar/Dockerfile
    # reads: services/calls/Dockerfile
    lines = (ROOT / dockerfile).read_text().replace("\\\n", " ").splitlines()
    final = []
    for line in (value.strip() for value in lines):
        if not line or line.startswith("#"):
            continue
        if line.upper().startswith("FROM "):
            final = []
        final.append(line)
    pair = ["ARG DEEDKIT_SOURCE_SHA", "ENV DEEDKIT_SOURCE_SHA=$DEEDKIT_SOURCE_SHA"]
    assert any(final[i:i + 2] == pair for i in range(len(final))) and sum("DEEDKIT_SOURCE_SHA" in line for line in final) == 2
    assert final.index(pair[0]) > max(i for i, line in enumerate(final) if line.startswith(("COPY ", "RUN ")))


# spec: ANYM-300
def test_the_runtime_image_smoke_imports_the_app_its_serve_command_loads():
    # `python -m messaging_service serve` imports messaging_service.app; importing only the package passed an image
    # whose API could not start (release N, 2026-10-28: platform_contracts.agent missing from the build context).
    assert p.BUILDS["runtime"][2:] == ("runtime", "import messaging_service.app, telegram_broker, telegram_session_owner, "
                                       "platform_contracts.release")
    assert "from .app import create_app" in (Path(p.__file__).parents[1] / "services/messaging/messaging_service/__main__.py").read_text()


def image_builder(monkeypatch, *, labels=None, env=None):
    calls, tagged = [], set()
    def command(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["docker", "tag"]:
            tagged.add(argv[-1])
        if argv[:3] == ["docker", "image", "ls"]:
            return IMAGE if argv[-1] in tagged else ""
        if argv[:3] == ["docker", "image", "inspect"]:
            if "--format" in argv:
                return IMAGE
            return json.dumps([{"Id": IMAGE, "Config": {
                "Labels": ImageStore.image("b")["Config"]["Labels"] if labels is None else labels,
                "Env": ["PATH=/usr/local/bin", "DEEDKIT_SOURCE_SHA=" + SHA] if env is None else env}}])
        return ""
    monkeypatch.setattr(p, "run", command)
    monkeypatch.setattr(p, "published", lambda target, sha: verified_image(target, IMAGE))
    return calls


@pytest.mark.parametrize("target", sorted(p.BUILDS))
def test_every_production_build_receives_the_candidate_source_revision(tmp_path, monkeypatch, target):
    if target in p.PUBLISHED:
        # CI built this target from the candidate revision; the host pulls that image by its verified digest.
        store = ImageStore()
        monkeypatch.setattr(p, "run", store.run)
        monkeypatch.setattr(p, "published", store.published)
        monkeypatch.setattr(p, "IMAGE_LOCK", tmp_path / "absent" / "image-build.lock")
        image = p.build_image(tmp_path, SHA, target)
        assert image == store.registry[target]["Id"] and store.built() == []
        assert [call[-1] for call in store.pulls()] == [store.reference(target)]
        assert store.images[image]["Config"]["Labels"]["org.opencontainers.image.revision"] == SHA
        assert ["docker", "image", "inspect", image] in store.calls
        assert any(call[:2] == ["docker", "run"] for call in store.calls)
        return
    calls = image_builder(monkeypatch)
    assert p.build_image(tmp_path, SHA, target) == IMAGE
    build = next(call for call in calls if call[:2] == ["docker", "build"])
    assert build[build.index("--build-arg") + 1] == "DEEDKIT_SOURCE_SHA=" + SHA
    assert "org.opencontainers.image.revision=" + SHA in build
    checked = ["docker", "image", "inspect", IMAGE] in calls
    assert checked == (target in MESSAGING_STAGES)
    assert any(call[:2] == ["docker", "run"] for call in calls)


@pytest.mark.parametrize("labels,env", [
    (None, ["PATH=/usr/local/bin"]),
    (None, ["DEEDKIT_SOURCE_SHA="]),
    (None, ["DEEDKIT_SOURCE_SHA=" + "c" * 40]),
    (None, []),
    ({"org.opencontainers.image.revision": "c" * 40}, None),
    ({}, None),
])
def test_new_messaging_image_without_the_candidate_revision_is_refused_before_smoke(tmp_path, monkeypatch, labels, env):
    calls = image_builder(monkeypatch, labels=labels, env=env)
    with pytest.raises(p.Refused, match="source revision"):
        p.build_image(tmp_path, SHA, "runtime")
    assert not any(call[:2] == ["docker", "run"] for call in calls)


def test_apply_still_accepts_prepared_images_built_before_the_source_revision(tmp_path, monkeypatch):
    settings, calls, _ = fixture(tmp_path, monkeypatch)
    p.prepare(settings, SHA)
    original = p.run
    def older(argv, **kw):
        # An image prepared before this change has the revision label but no DEEDKIT_SOURCE_SHA.
        if argv[:3] == ["docker", "image", "inspect"] and "--format" not in argv:
            return json.dumps([{"Id": IMAGE, "Config": {"Labels": {"org.opencontainers.image.revision": SHA},
                                                        "Env": ["PATH=/usr/local/bin"]}}])
        return original(argv, **kw)
    monkeypatch.setattr(p, "run", older)
    assert p.apply(settings, SHA)["status"] == "applied"



class ImageStore:
    """Synthetic Docker image store: a tag names one image; a rebuild moves the tag and drops the old image.

    `registry` is the synthetic GHCR: the image CI published for each target, which `published` reports as the
    attestation verifier would (classic image store: the image ID is the config digest)."""

    def __init__(self, tags=None, registry=None):
        self.tags = dict(tags or {})
        self.images = {value["Id"]: value for value in self.tags.values()}
        self.registry = dict(registry or {})
        self.calls = []

    @staticmethod
    def manifest(target):
        return "sha256:" + hashlib.sha256(("manifest " + target).encode()).hexdigest()

    def reference(self, target):
        return f"{p.REGISTRY}{target}@{self.manifest(target)}"

    def published(self, target, sha):
        if target not in self.registry:
            image = ImageStore.image("c", target, revision=sha, source=sha)
            self.registry[target] = {**image, "Id": "sha256:" + hashlib.sha256(("ci " + target).encode()).hexdigest()}
        return verified_image(target, self.registry[target]["Id"], self.manifest(target))

    def pulled(self, reference):
        return next(value for target, value in self.registry.items() if self.reference(target) == reference)

    def pulls(self):
        return [call for call in self.calls if call[:2] == ["docker", "pull"]]

    @staticmethod
    def image(seed, target="runtime", *, revision=SHA, source=SHA, labels=None):
        """An image as production.py builds it for `target`, unless `labels` overrides its labels."""
        value = {"org.opencontainers.image.revision": revision,
                 "org.opencontainers.image.source": "https://github.com/" + p.REPOSITORY,
                 "io.deedkit.production.build": p.build_recipe(target, revision)[1]}
        return {"Id": "sha256:" + seed * 64, "Config": {"Labels": value if labels is None else labels,
                                                       "Env": ["PATH=/usr/local/bin", "DEEDKIT_SOURCE_SHA=" + source]}}

    def run(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[:3] == ["docker", "image", "ls"]:
            return self.tags[argv[-1]]["Id"] + "\n" if argv[-1] in self.tags else ""
        if argv[:2] == ["docker", "build"]:
            tag = argv[argv.index("--tag") + 1]
            labels = dict(argv[i + 1].split("=", 1) for i, value in enumerate(argv) if value == "--label")
            env = [argv[i + 1] for i, value in enumerate(argv) if value == "--build-arg" and argv[i + 1].startswith("DEEDKIT_")]
            fresh = {"Id": "sha256:" + format(len(self.calls), "x").rjust(64, "f"), "Config": {"Labels": labels, "Env": env}}
            self.images.pop(self.tags.get(tag, {}).get("Id"), None)
            self.tags[tag] = self.images[fresh["Id"]] = fresh
            return ""
        if argv[:2] == ["docker", "pull"]:
            value = self.pulled(argv[-1])
            self.images[value["Id"]] = value
            return ""
        if argv[:2] == ["docker", "tag"]:
            self.tags[argv[-1]] = self.pulled(argv[-2])
            return ""
        if argv[:3] == ["docker", "image", "inspect"]:
            value = self.tags.get(argv[-1]) or (self.pulled(argv[-1]) if "@" in argv[-1] else self.images[argv[-1]])
            return value["Id"] if "--format" in argv else json.dumps([value])
        if argv[:2] == ["docker", "run"] and not any(image in argv for image in self.images):
            raise p.Refused("docker failed (exit 125)")  # No such image
        return ""

    def built(self):
        return [call for call in self.calls if call[:2] == ["docker", "build"]]


@pytest.fixture
def no_host_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "IMAGE_LOCK", tmp_path / "absent" / "image-build.lock")


@pytest.mark.parametrize("target", sorted(p.BUILDS))
def test_existing_tag_naming_the_revision_is_reused_without_a_rebuild(tmp_path, monkeypatch, no_host_lock, target):
    existing = ImageStore.image("e", target)
    # A CI-published target's tag is reused only because it names the verified CI image (an earlier pull).
    store = ImageStore({f"deedkit-production-{target}:{SHA}": existing}, {target: existing})
    monkeypatch.setattr(p, "run", store.run)
    monkeypatch.setattr(p, "published", store.published)
    assert p.build_image(tmp_path, SHA, target) == existing["Id"]
    assert store.built() == [] and store.pulls() == [] and not any(call[:2] == ["docker", "tag"] for call in store.calls)
    smoke = next(call for call in store.calls if call[:2] == ["docker", "run"])
    assert existing["Id"] in smoke and smoke[smoke.index("--network") + 1] == "none"


@pytest.mark.parametrize("target, existing", [
    ("runtime", ImageStore.image("e", revision="c" * 40, source="c" * 40)),
    ("gateway", ImageStore.image("e", "gateway", revision="c" * 40)),
    # An image of this revision built before DEEDKIT_SOURCE_SHA existed, or naming another source.
    ("runtime", {"Id": "sha256:" + "e" * 64, "Config": {"Labels": {"org.opencontainers.image.revision": SHA}}}),
    ("backup", ImageStore.image("e", "backup", source="c" * 40)),
])
def test_existing_tag_that_does_not_name_the_revision_is_refused_without_a_rebuild(tmp_path, monkeypatch, no_host_lock,
                                                                                    target, existing):
    tag = f"deedkit-production-{target}:{SHA}"
    store = ImageStore({tag: existing})
    monkeypatch.setattr(p, "run", store.run)
    monkeypatch.setattr(p, "published", store.published)
    with pytest.raises(p.Refused, match="does not name the candidate"):
        p.build_image(tmp_path, SHA, target)
    # The image keeps its tag, so a container that uses it still resolves it.
    assert store.built() == [] and store.tags[tag] is existing and existing["Id"] in store.images
    assert store.pulls() == []
    assert not any(call[:2] == ["docker", "run"] for call in store.calls)


def recipe_labels(target, **change):
    labels = ImageStore.image("e", target)["Config"]["Labels"]
    return {key: value for key, value in {**labels, **change}.items() if value is not None}


@pytest.mark.parametrize("target, labels", [
    # Built by another tool or for another target, stage or build arguments, or from another source.
    ("gateway", recipe_labels("gateway", **{"io.deedkit.production.build": None})),
    ("runtime", recipe_labels("runtime", **{"io.deedkit.production.build": p.build_recipe("indexer", SHA)[1]})),
    ("calendar", recipe_labels("calendar", **{"io.deedkit.production.build": "0" * 64})),
    ("http-router", recipe_labels("http-router", **{"org.opencontainers.image.source": None})),
])
def test_existing_tag_from_another_build_recipe_is_refused_without_a_rebuild(tmp_path, monkeypatch, no_host_lock,
                                                                             target, labels):
    tag = f"deedkit-production-{target}:{SHA}"
    existing = ImageStore.image("e", target, labels=labels)
    store = ImageStore({tag: existing})
    monkeypatch.setattr(p, "run", store.run)
    monkeypatch.setattr(p, "published", store.published)
    with pytest.raises(p.Refused, match="does not name the candidate"):
        p.build_image(tmp_path, SHA, target)
    assert store.built() == [] and store.pulls() == [] and store.tags[tag] is existing


def test_ambiguous_existing_tag_is_refused_without_a_rebuild(tmp_path, monkeypatch, no_host_lock):
    store = ImageStore()
    def listing(argv, **kwargs):
        if argv[:3] == ["docker", "image", "ls"]:
            return "sha256:" + "e" * 64 + "\nsha256:" + "d" * 64 + "\n"
        return store.run(argv, **kwargs)
    monkeypatch.setattr(p, "run", listing)
    monkeypatch.setattr(p, "published", store.published)
    with pytest.raises(p.Refused, match="ambiguous"):
        p.build_image(tmp_path, SHA, "runtime")
    assert store.built() == [] and store.pulls() == []


@pytest.mark.parametrize("target", sorted(p.BUILDS))
def test_second_preparation_of_a_revision_keeps_the_image_the_first_one_built(tmp_path, monkeypatch, no_host_lock, target):
    store = ImageStore()
    monkeypatch.setattr(p, "run", store.run)
    monkeypatch.setattr(p, "published", store.published)
    first = p.build_image(tmp_path, SHA, target)
    assert p.build_image(tmp_path, SHA, target) == first
    # The first preparation pulled a CI-published target (or built http-router); the second reuses it.
    assert len(store.built() + store.pulls()) == 1 and first in store.images


def test_image_lookup_and_build_hold_the_host_image_lock(tmp_path, monkeypatch):
    import fcntl
    monkeypatch.setattr(p, "IMAGE_LOCK", tmp_path / "image-build.lock")
    store = ImageStore()
    held = []
    def run(argv, **kwargs):
        if argv[:2] in (["docker", "build"], ["docker", "pull"]) or argv[:3] == ["docker", "image", "ls"]:
            with (tmp_path / "image-build.lock").open("a") as probe:
                try:
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    held.append(False)
                except BlockingIOError:
                    held.append(True)
        return store.run(argv, **kwargs)
    monkeypatch.setattr(p, "run", run)
    monkeypatch.setattr(p, "published", store.published)
    p.build_image(tmp_path, SHA, "runtime")
    # The lookup, the pull and the check that the new tag names the pulled image.
    assert held == [True, True, True]
    # A concurrent preparation (another tool or state directory) is refused before its lookup.
    with (tmp_path / "image-build.lock").open("a") as other:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        calls = len(store.calls)
        with pytest.raises(p.Refused, match="image lock"):
            p.build_image(tmp_path, "c" * 40, "runtime")
        assert len(store.calls) == calls


def test_container_check_builds_the_working_tree_without_touching_production_tags(tmp_path, monkeypatch, no_host_lock):
    production = ImageStore.image("e")
    store = ImageStore({f"deedkit-production-runtime:{SHA}": production})
    monkeypatch.setattr(p, "run", store.run)
    checked = p.build_image(tmp_path, SHA, "runtime", check=True)
    assert checked != production["Id"] and store.tags[f"deedkit-production-runtime:{SHA}"] is production
    assert [call[call.index("--tag") + 1] for call in store.built()] == [f"deedkit-check-runtime:{SHA}"]
    assert not any(call[:3] == ["docker", "image", "ls"] for call in store.calls)
    p.build_image(tmp_path, SHA, "runtime", check=True)
    assert len(store.built()) == 2


def queue_github(*, push_verify="skipped", push_native="success", queue=None, queue_jobs=None, extra_queue_runs=(), pages=None,
                 changes=None):
    """A main push run that skipped checks / verify plus the merge queue run of the same commit."""
    push = {"id": 40, "run_attempt": 1, "head_sha": SHA, "head_branch": "main", "event": "push", "status": "completed",
            "conclusion": "success", "head_repository": {"full_name": p.REPOSITORY}, "html_url": "https://example.invalid/run/40"}
    group = {"id": 30, "run_attempt": 2, "head_sha": SHA, "head_branch": "gh-readonly-queue/main/pr-86-" + "c" * 40,
             "event": "merge_group", "status": "completed", "conclusion": "success",
             "head_repository": {"full_name": p.REPOSITORY}, "html_url": "https://example.invalid/run/30", **(queue or {})}
    push_jobs = [{"name": name, "status": "completed",
                  "conclusion": {"checks / verify": push_verify, "native / synthetic": push_native}.get(name, "success")}
                 for name in p.REQUIRED_JOBS]
    group_jobs = queue_jobs if queue_jobs is not None else [
        {"name": name, "status": "completed", "conclusion": "success"} for name in ("checks / verify", "CI gate", "checks / load")]
    reads = {"push": 0, "merge_group": 0}

    def get(path):
        if path.startswith("compare/main..."):
            return comparison(path.removeprefix("compare/main..."))
        if "/runs?" in path:
            event = "merge_group" if "event=merge_group" in path else "push"
            reads[event] += 1
            runs = [copy.deepcopy(push)] if event == "push" else [copy.deepcopy(group), *extra_queue_runs]
            if changes and reads[event] > 1:
                for run in runs:
                    run.update(changes.get(event, {}))
            if pages and event == "merge_group":
                page = int(re.search(r"[?&]page=(\d+)", path).group(1))
                return {"workflow_runs": pages[page - 1] if page <= len(pages) else [], "total_count": sum(map(len, pages))}
            return {"workflow_runs": runs}
        if "runs/40/attempts/1/jobs?" in path:
            return {"jobs": push_jobs}
        assert "runs/30/attempts/2/jobs?" in path, path
        return {"jobs": group_jobs}
    return get, push, group


def test_main_push_that_reused_the_queue_verification_of_the_same_commit_is_promotable():
    get, push, group = queue_github()
    result = p.verify(SHA, get)
    # The push run stays the promoted run; the queue run is named as the source of the test verification.
    assert (result["run_id"], result["run_attempt"], result["url"]) == (40, 1, push["html_url"])
    assert (result["verify_run_id"], result["verify_run_attempt"], result["verify_run_url"]) == (30, 2, group["html_url"])
    # A push run that ran checks / verify itself keeps today's path and names itself as the source.
    plain, _, _ = github()
    assert p.verify(SHA, plain)["verify_run_id"] == 12


QUEUE_JOBS_WITH_NATIVE = [{"name": name, "status": "completed", "conclusion": "success"}
                          for name in ("checks / verify", "CI gate", "checks / load", "native / synthetic")]


# spec: ANYM-321
def test_a_push_run_that_reused_the_queue_may_skip_native_only_when_the_queue_run_passed_it():
    get, _, group = queue_github(push_native="skipped", queue_jobs=QUEUE_JOBS_WITH_NATIVE)
    assert p.verify(SHA, get)["verify_run_id"] == group["id"]
    for conclusion in ("failure", "skipped", "cancelled", "missing"):
        jobs = [job if job["name"] != "native / synthetic" else {**job, "conclusion": conclusion}
                for job in QUEUE_JOBS_WITH_NATIVE if conclusion != "missing" or job["name"] != "native / synthetic"]
        get, _, _ = queue_github(push_native="skipped", queue_jobs=jobs)
        with pytest.raises(p.Refused, match="merge queue job did not pass: native / synthetic"):
            p.verify(SHA, get)
    # Without the queue reuse of checks / verify a skipped native job never counts, whatever the queue run shows.
    get, _, _ = queue_github(push_verify="success", push_native="skipped", queue_jobs=QUEUE_JOBS_WITH_NATIVE)
    with pytest.raises(p.Refused, match="required job did not pass: native / synthetic"):
        p.verify(SHA, get)
    # A push run that ran native itself still needs no native job in the queue run.
    get, _, _ = queue_github()
    assert p.verify(SHA, get)["verify_run_id"] == group["id"]


@pytest.mark.parametrize("case", [
    {"queue": {"conclusion": "failure"}}, {"queue": {"conclusion": "cancelled"}}, {"queue": {"status": "in_progress"}},
    {"queue": {"head_repository": {"full_name": "fork/deedkit"}}}, {"queue": {"head_branch": "gh-readonly-queue/other/pr-1"}},
    {"queue": {"head_branch": "feature"}}, {"queue": {"head_sha": "b" * 40}}, {"queue": {"event": "push"}},
    {"queue_jobs": [{"name": "checks / verify", "status": "completed", "conclusion": "skipped"},
                    {"name": "CI gate", "status": "completed", "conclusion": "success"}]},
    {"queue_jobs": [{"name": "CI gate", "status": "completed", "conclusion": "success"}]},
    {"queue_jobs": [{"name": "checks / verify", "status": "completed", "conclusion": "success"}]},
    {"queue_jobs": [{"name": "checks / verify", "status": "completed", "conclusion": "success"}] * 2
                   + [{"name": "CI gate", "status": "completed", "conclusion": "success"}]},
    {"push_verify": "failure"}, {"push_verify": "cancelled"},
], ids=["queue-failed", "queue-cancelled", "queue-pending", "other-repository", "other-base-branch", "not-a-queue-branch",
        "other-commit", "not-a-queue-event", "queue-verify-skipped", "queue-verify-missing", "queue-gate-missing",
        "queue-verify-duplicated", "push-verify-failed", "push-verify-cancelled"])
def test_queue_verification_never_stands_in_for_a_failed_unrelated_or_skipped_run(case):
    get, _, _ = queue_github(**case)
    with pytest.raises(p.Refused):
        p.verify(SHA, get)


def test_a_newer_failed_queue_run_supersedes_an_older_green_one_and_no_queue_run_refuses():
    _, _, group = queue_github()
    newer = {**group, "id": 31, "conclusion": "failure"}
    get, _, _ = queue_github(extra_queue_runs=[newer])
    with pytest.raises(p.Refused, match="merge queue run"):
        p.verify(SHA, get)
    empty, _, _ = queue_github(pages=[[]])
    with pytest.raises(p.Refused, match="no merge queue run"):
        p.verify(SHA, empty)


@pytest.mark.parametrize("changes", [{"merge_group": {"run_attempt": 3}}, {"merge_group": {"conclusion": "failure"}},
                                     {"push": {"run_attempt": 2}}, {"push": {"status": "in_progress"}}],
                         ids=["queue-rerun", "queue-result-changed", "push-rerun", "push-rerun-pending"])
def test_a_rerun_between_the_reads_is_refused(changes):
    get, _, _ = queue_github(changes=changes)
    with pytest.raises(p.Refused, match="changed while"):
        p.verify(SHA, get)


def test_run_listings_are_followed_across_pages_or_refused():
    _, _, group = queue_github()
    older = [{**group, "id": n, "conclusion": "success"} for n in range(1, 101)]
    # The newest (failed) queue run is on the second page: it must still supersede the green ones.
    get, _, _ = queue_github(pages=[older, [{**group, "id": 500, "conclusion": "failure"}]])
    with pytest.raises(p.Refused, match="merge queue run"):
        p.verify(SHA, get)
    # A listing that claims more runs than it returns is refused rather than judged on a partial view.
    truncated, _, _ = queue_github(pages=[older])
    def short(path):
        value = truncated(path)
        if "event=merge_group" in path:
            value = {**value, "total_count": 250}
        return value
    with pytest.raises(p.Refused, match="completely"):
        p.verify(SHA, short)


@pytest.mark.parametrize("pages", [
    [{"workflow_runs": [{"id": n} for n in range(100)]}, {"workflow_runs": [{"id": 100}], "total_count": 5}],
    [{"workflow_runs": [{"id": 1}], "total_count": 2}, {"workflow_runs": [{"id": 1}], "total_count": 2}],
    [{"workflow_runs": [{"id": 1}], "total_count": 3}, {"workflow_runs": [{"id": 2}], "total_count": 2}],
    [{"workflow_runs": [{"id": 1}, {"id": 2}], "total_count": 1}],
    [{"workflow_runs": [{"id": 1}], "total_count": "1"}],
    [{"workflow_runs": [{"id": n} for n in range(100)]}] * 11,
], ids=["count-appears-late", "repeated-page", "changing-total", "more-than-total", "invalid-total", "endless-full-pages"])
def test_incomplete_or_inconsistent_run_listings_are_refused(pages):
    responses = iter(pages + [{"workflow_runs": []}] * 20)
    with pytest.raises(p.Refused, match="CI listing"):
        p.complete_listing(lambda page: next(responses), "workflow_runs")


def test_a_full_first_page_without_a_count_is_followed_to_the_next_page():
    first = [{"id": n} for n in range(100)]
    pages = {1: {"workflow_runs": first}, 2: {"workflow_runs": [{"id": 500}]}}
    assert len(p.complete_listing(lambda page: pages[page], "workflow_runs")) == 101


def test_a_job_listing_that_stops_short_of_its_total_never_hides_a_duplicate_required_job():
    passed = [{"id": 1, "name": "checks / verify", "status": "completed", "conclusion": "success"},
              {"id": 2, "name": "CI gate", "status": "completed", "conclusion": "success"}]
    hidden = {"id": 3, "name": "checks / verify", "status": "completed", "conclusion": "failure"}
    for second_page in ([], [hidden]):
        get, _, _ = queue_github()
        def response(path, second_page=second_page):
            if "runs/30/attempts/2/jobs?" in path:
                return {"jobs": passed, "total_count": 3} if path.endswith("&page=1") else {"jobs": second_page, "total_count": 3}
            return get(path)
        with pytest.raises(p.Refused):
            p.verify(SHA, response)


@pytest.mark.parametrize("event", ["merge_group", "push"])
def test_a_newer_run_appearing_between_the_reads_is_refused(event):
    get, push, group = queue_github()
    reads = {"push": 0, "merge_group": 0}
    def response(path):
        if "/runs?" in path:
            kind = "merge_group" if "event=merge_group" in path else "push"
            reads[kind] += 1
            if kind == event and reads[kind] > 1:
                # Same attempt, status and conclusion, only a newer run of the same commit.
                newer = {**(group if kind == "merge_group" else push), "id": 99}
                return {"workflow_runs": [push if kind == "push" else group, newer]}
        return get(path)
    with pytest.raises(p.Refused, match="changed while"):
        p.verify(SHA, response)


def test_after_a_reset_the_newest_push_run_of_the_commit_decides_whether_the_queue_counts():
    get, push, group = queue_github()
    ran_itself = {**push, "id": 20, "html_url": "https://example.invalid/run/20"}
    def response(path, push_runs):
        if "/runs?" in path and "event=push" in path:
            return {"workflow_runs": push_runs}
        if "runs/20/attempts/1/jobs?" in path:
            return {"jobs": [{"name": name, "status": "completed", "conclusion": "success"} for name in p.REQUIRED_JOBS]}
        return get(path)
    # The newest push run (40) skipped verify: an older exact-commit queue run supplies it, however old.
    reused = p.verify(SHA, lambda path: response(path, [ran_itself, push]))
    assert reused["run_id"] == 40 and reused["verify_run_id"] == 30
    # The newest push run (20 renumbered newest) ran verify itself: only its own result counts.
    newest_ran = {**ran_itself, "id": 50}
    def own(path):
        if "runs/50/attempts/1/jobs?" in path:
            path = path.replace("runs/50/", "runs/20/")
        return response(path, [push, newest_ran])
    assert p.verify(SHA, own)["verify_run_id"] == 50


def test_a_truncated_push_job_listing_never_hides_a_failed_required_job():
    get, _, _ = github()
    passed = [{"id": n, "name": name, "status": "completed", "conclusion": "success"}
              for n, name in enumerate(sorted(p.REQUIRED_JOBS))]
    hidden = {"id": 99, "name": "native / synthetic", "status": "completed", "conclusion": "failure"}
    for second_page in ([], [hidden]):
        def response(path, second_page=second_page):
            if "runs/12/attempts/2/jobs?" in path:
                total = len(passed) + 1
                return {"jobs": passed, "total_count": total} if path.endswith("&page=1") else {"jobs": second_page, "total_count": total}
            return get(path)
        with pytest.raises(p.Refused):
            p.verify(SHA, response)


def test_published_targets_are_the_publication_matrix_and_the_verifier_is_the_recorded_build():
    from scripts import publish_images
    assert p.PUBLISHED == publish_images.TARGETS and set(p.BUILDS) - set(p.PUBLISHED) == {"http-router", "whatsapp-holder"}
    recorded = (ROOT / "tools/attest-verify/attest-verify.linux-amd64.sha256").read_text().split()
    assert recorded == [p.VERIFIER_SHA256, "attest-verify"]


@pytest.mark.parametrize("outcome", ["failure", "skipped", "cancelled", None, "missing", "duplicate"])
def test_main_run_without_a_passed_publish_job_is_refused(outcome):
    get, _, jobs = github()
    # The run's jobs are listed explicitly, so the case does not depend on REQUIRED_JOBS naming `publish`.
    jobs[:] = [job for job in jobs if job["name"] != "publish"]
    publish = {"name": "publish", "status": "completed", "conclusion": "success"}
    if outcome == "duplicate":
        jobs += [publish, copy.deepcopy(publish)]
    elif outcome != "missing":
        jobs.append({**publish, "conclusion": outcome})
    with pytest.raises(p.Refused, match="required job did not pass: publish"):
        p.verify(SHA, get)


def test_host_built_image_with_matching_labels_is_refused_instead_of_the_ci_image(tmp_path, monkeypatch, no_host_lock):
    # The old host-building tool left a tag with the right revision, source and recipe labels but other content.
    tag = f"deedkit-production-runtime:{SHA}"
    host_built = ImageStore.image("e")
    store = ImageStore({tag: host_built})
    monkeypatch.setattr(p, "run", store.run)
    monkeypatch.setattr(p, "published", store.published)
    with pytest.raises(p.Refused, match="not the verified CI image"):
        p.build_image(tmp_path, SHA, "runtime")
    assert store.tags[tag] is host_built and store.pulls() == [] and store.built() == []
    assert not any(call[:2] in (["docker", "tag"], ["docker", "run"]) for call in store.calls)


def test_existing_tag_of_the_pulled_manifest_is_reused_with_the_containerd_image_store(tmp_path, monkeypatch, no_host_lock):
    # With the containerd image store the image ID is the manifest digest, not the config digest.
    tag = f"deedkit-production-gateway:{SHA}"
    pulled = {**ImageStore.image("e", "gateway"), "Id": ImageStore.manifest("gateway")}
    store = ImageStore({tag: pulled}, {"gateway": ImageStore.image("c", "gateway")})
    monkeypatch.setattr(p, "run", store.run)
    monkeypatch.setattr(p, "published", store.published)
    assert p.build_image(tmp_path, SHA, "gateway") == pulled["Id"]
    assert store.pulls() == [] and store.built() == []


@pytest.mark.parametrize("change", ["other-image", "other-revision"])
def test_pulled_image_that_is_not_the_verified_ci_image_is_never_tagged(tmp_path, monkeypatch, no_host_lock, change):
    store = ImageStore()
    monkeypatch.setattr(p, "run", store.run)
    verified = store.published("runtime", SHA)
    if change == "other-image":
        # The registry served other content than the verifier saw.
        monkeypatch.setattr(p, "published", lambda target, sha: {**verified, "config_digest": "sha256:" + "0" * 64})
        match = "not the verified digest"
    else:
        store.registry["runtime"]["Config"]["Labels"]["org.opencontainers.image.revision"] = "c" * 40
        monkeypatch.setattr(p, "published", store.published)
        match = "does not name the candidate"
    with pytest.raises(p.Refused, match=match):
        p.build_image(tmp_path, SHA, "runtime")
    assert f"deedkit-production-runtime:{SHA}" not in store.tags
    assert not any(call[:2] in (["docker", "tag"], ["docker", "run"]) for call in store.calls)


def fake_verifier(tmp_path, monkeypatch, content=b"#!/bin/sh\n"):
    path = tmp_path / "attest-verify"
    path.write_bytes(content)
    path.chmod(0o755)
    monkeypatch.setattr(p, "VERIFIER", path)
    monkeypatch.setattr(p, "VERIFIER_SHA256", hashlib.sha256(content).hexdigest())
    return path


def test_verifier_runs_without_any_credential_and_its_result_is_bound_to_target_and_revision(tmp_path, monkeypatch):
    path = fake_verifier(tmp_path, monkeypatch)
    monkeypatch.setenv("GH_TOKEN", "synthetic-token")
    seen = []
    def run(argv, **kwargs):
        seen.append((argv, kwargs))
        return json.dumps({**verified_image("calls", IMAGE), "sha": SHA}) + "\n"
    monkeypatch.setattr(p, "run", run)
    assert p.published("calls", SHA) == verified_image("calls", IMAGE)
    argv, kwargs = seen[0]
    assert argv == [str(path), "--image", "ghcr.io/gosync-inc/deedkit-calls", "--sha", SHA]
    assert kwargs["env"] == {"PATH": "/usr/bin:/bin"} and kwargs["reason"] is True


@pytest.mark.parametrize("output", [
    "not json", "[]", json.dumps({**verified_image("calls", IMAGE), "sha": "c" * 40}),
    json.dumps({**verified_image("gateway", IMAGE), "sha": SHA}),
    json.dumps({**verified_image("calls", IMAGE), "reference": "ghcr.io/other/deedkit-calls@" + MANIFEST, "sha": SHA}),
    json.dumps({**verified_image("calls", IMAGE), "digest": "sha256:" + "1" * 64, "sha": SHA}),
    json.dumps({**verified_image("calls", "latest"), "sha": SHA}),
], ids=["not-json", "not-object", "other-revision", "other-target", "other-registry", "other-digest", "no-config-digest"])
def test_a_verifier_result_for_another_target_revision_or_digest_is_refused(tmp_path, monkeypatch, output):
    fake_verifier(tmp_path, monkeypatch)
    monkeypatch.setattr(p, "run", lambda argv, **kwargs: output)
    with pytest.raises(p.Refused, match="no verified reference"):
        p.published("calls", SHA)


@pytest.mark.parametrize("case", ["missing", "other-build", "group-writable", "symlink"])
def test_only_the_pinned_verifier_build_is_executed(tmp_path, monkeypatch, case):
    path = fake_verifier(tmp_path, monkeypatch)
    if case == "missing":
        path.unlink()
    elif case == "other-build":
        path.write_bytes(b"#!/bin/sh\necho accepted\n")
    elif case == "group-writable":
        path.chmod(0o775)
    else:
        link = tmp_path / "link"
        link.symlink_to(path)
        monkeypatch.setattr(p, "VERIFIER", link)
    monkeypatch.setattr(p, "run", lambda argv, **kwargs: pytest.fail("an unpinned verifier ran"))
    with pytest.raises(p.Refused, match="attestation verifier"):
        p.published("runtime", SHA)


def test_a_verifier_refusal_passes_its_reason_on_and_stops_the_target(tmp_path, monkeypatch):
    path = fake_verifier(tmp_path, monkeypatch, b"#!/bin/sh\necho 'attest-verify: refused: expected SourceRepositoryDigest' >&2\nexit 1\n")
    monkeypatch.setattr(p, "VERIFIER_SHA256", hashlib.sha256(path.read_bytes()).hexdigest())
    with pytest.raises(p.Refused, match="attest-verify failed \\(exit 1\\): attest-verify: refused: expected SourceRepositoryDigest"):
        p.published("runtime", SHA)


def test_every_published_target_is_verified_before_the_first_pull(tmp_path, monkeypatch):
    settings, calls, document = fixture(tmp_path, monkeypatch)
    settings["projects"][0]["services"]["worker"] = "indexer"
    document["services"]["worker"] = copy.deepcopy(document["services"]["api"])
    (tmp_path / "original.json").write_text(json.dumps(document))
    def published(target, sha):
        if target == "runtime":
            raise p.Refused("attest-verify failed (exit 1): attest-verify: refused: self-hosted runner")
        return verified_image(target, IMAGE)
    monkeypatch.setattr(p, "published", published)
    with pytest.raises(p.Refused, match="self-hosted"):
        p.prepare(settings, SHA)
    assert not any(call[:2] in (["docker", "pull"], ["docker", "build"], ["docker", "tag"]) for call in calls)
    assert not (Path(settings["state_directory"]) / SHA / "prepared.json").exists()


class Host:
    """A synthetic production host: one Compose project whose `api` service runs on one container at a time,
    and a synthetic GHCR that publishes one runtime image per revision. No Docker or network access."""

    def __init__(self, tmp_path, monkeypatch):
        self.root = tmp_path
        self.state = tmp_path / "state"
        self.state.mkdir(mode=0o700)
        self.original = tmp_path / "original.json"
        self.original.write_text(json.dumps({"name": "synthetic", "services": {
            "api": {"image": "old", "build": {"context": "/unused"}, "environment": {"SYNTHETIC": "literal"},
                    "volumes": [{"source": "/existing", "target": "/data"}]}, "db": {"image": "postgres:pinned"}}}))
        self.project = {"name": "synthetic", "directory": str(tmp_path), "files": [str(self.original)],
                        "services": {"api": "runtime"}}
        self.settings = {"version": 1, "state_directory": str(self.state), "projects": [self.project]}
        self.images = {OLD_IMAGE: {"Id": OLD_IMAGE, "Config": {"Labels": {}}}}
        self.tags, self.calls, self.main, self.serial = {}, [], SHA, 0
        self.container = {"Id": "container0", "Image": OLD_IMAGE, "State": {"Running": True}}
        monkeypatch.setattr(p, "run", self.run)
        monkeypatch.setattr(p, "promotable", lambda sha: {"sha": sha, "run_id": 12})
        monkeypatch.setattr(p, "published", self.published)
        monkeypatch.setattr(p, "check_space", lambda directory: None)
        monkeypatch.setattr(p, "IMAGE_LOCK", tmp_path / "absent" / "image-build.lock")

    @staticmethod
    def image(sha):
        return "sha256:" + hashlib.sha256(("ci " + sha).encode()).hexdigest()

    def published(self, target, sha):
        return verified_image(target, self.image(sha), "sha256:" + hashlib.sha256(sha.encode()).hexdigest())

    def run(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[0] == "git":
            return self.main if "FETCH_HEAD" in argv else ""
        if argv[:2] == ["docker", "info"]:
            return str(self.root)
        if argv[:2] == ["docker", "pull"]:
            sha = next(sha for sha in (self.main,) if argv[-1].endswith(hashlib.sha256(sha.encode()).hexdigest()))
            self.images[self.image(sha)] = ImageStore.image("c", revision=sha, source=sha) | {"Id": self.image(sha)}
            return ""
        if argv[:2] == ["docker", "tag"]:
            self.tags[argv[-1]] = self.image(self.main)
            return ""
        if argv[:3] == ["docker", "image", "ls"]:
            return self.tags.get(argv[-1], "")
        if argv[:3] == ["docker", "image", "inspect"]:
            name = self.image(self.main) if "@" in argv[-1] else argv[-1]
            if name not in self.images:
                raise p.Refused("docker failed (exit 1)")  # No such image
            return name if "--format" in argv else json.dumps([self.images[name]])
        if argv[:2] == ["docker", "compose"]:
            if "config" in argv:
                return json.dumps(json.loads(Path(argv[argv.index("-f") + 1]).read_text())).replace("$$", "$")
            if "stop" in argv:
                self.container["State"] = {"Running": False}
            if "up" in argv:
                self.serial += 1
                image = json.loads(Path(argv[argv.index("-f") + 1]).read_text())["services"]["api"]["image"]
                self.container = {"Id": f"container{self.serial}", "Image": image, "State": {"Running": True}}
            if "ps" in argv:
                return self.container["Id"] + "\n"
            return ""
        if argv[:2] == ["docker", "ps"]:
            # Like Docker: without --all only running (or restarting) containers are listed.
            return self.container["Id"] + "\n" if "--all" in argv or self.container["State"].get("Running") else ""
        if argv[:2] == ["docker", "inspect"]:
            assert argv[2:] == [self.container["Id"]]
            return json.dumps([self.container])
        return ""

    def release(self, sha):
        self.main = sha
        p.prepare(self.settings, sha)
        return p.apply(self.settings, sha)

    def mutations(self):
        return [call for call in self.calls if call[:2] == ["docker", "compose"] and ("stop" in call or "up" in call)]


RELEASES = ("1" * 40, "2" * 40, "3" * 40)


def test_receipts_record_the_verified_reference_applied_at_and_rollback_data(tmp_path, monkeypatch):
    host = Host(tmp_path, monkeypatch)
    sha = RELEASES[0]
    host.main = sha
    prepared = p.prepare(host.settings, sha)
    reference = host.published("runtime", sha)["reference"]
    assert prepared["references"] == {"runtime": reference}
    result = p.apply(host.settings, sha)
    assert result["references"] == {"runtime": reference} and p.read(host.state / "current.json") == result
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", result["applied_at"])
    [project] = result["rollback"]["projects"]
    assert result["rollback"]["version"] == 1 and project["services"] == ["api"]
    assert project["before_file_digest"] == p.digest(p.read(host.state / sha / "synthetic.before.json"))
    assert project["running"] == {"api": {"container": "container1", "image": host.image(sha),
                                          "settings": p.container_settings(host.container)}}
    assert project["after_digest"] == p.digest(json.loads(host.original.read_text()))


def test_three_successive_releases_roll_back_only_the_current_one_once(tmp_path, monkeypatch):
    host = Host(tmp_path, monkeypatch)
    for sha in RELEASES:
        host.release(sha)
    assert host.container["Image"] == host.image(RELEASES[2])
    host.calls.clear()
    outcome = p.rollback(host.settings, RELEASES[2])
    stop = next(i for i, call in enumerate(host.calls) if "stop" in call)
    up = next(i for i, call in enumerate(host.calls) if "up" in call)
    assert stop < up and host.calls[up][-1] == "api" and "--no-deps" in host.calls[up] and "--no-build" in host.calls[up]
    assert host.calls[up][host.calls[up].index("--pull") + 1] == "never"
    assert host.calls[up][host.calls[up].index("-f") + 1] == str(host.state / RELEASES[2] / "synthetic.before.json")
    assert not any(call[:2] == ["docker", "pull"] or "db" in call for call in host.calls)
    # The release before it runs again, recorded as such.
    assert host.container["Image"] == host.image(RELEASES[1]) and host.container["State"]["Running"]
    assert outcome["previous_images"] == {"synthetic": {"api": host.image(RELEASES[1])}}
    current = p.read(host.state / "current.json")
    assert current["status"] == "rolled-back" and current["previous_images"] == outcome["previous_images"]
    assert p.read(host.state / RELEASES[2] / "rollback-attempted.json")["status"] == "rolling-back"
    assert p.read(host.state / RELEASES[2] / "rollback.json") == outcome
    host.calls.clear()
    for sha in (RELEASES[2], RELEASES[1]):
        with pytest.raises(p.Refused, match="only the current applied release"):
            p.rollback(host.settings, sha)
    assert host.mutations() == []


# ANYM-895: a synthetic checkout whose Dockerfiles copy what the real ones copy for these targets, in miniature.
KEEP_MESSAGING_DOCKERFILE = """ARG PYTHON_IMAGE=python:3.13-slim@sha256:{base}
FROM ${{PYTHON_IMAGE}} AS python-dependencies
COPY services/messaging/requirements.lock ./requirements.lock
RUN pip install -r requirements.lock
FROM golang:1@sha256:{go} AS whatsmeow-build
COPY whatsapp/whatsmeow ./
RUN go build -o /out/anymsg-whatsmeow .
FROM python-dependencies AS runtime
COPY services/messaging/messaging_service ./messaging_service
FROM python-dependencies AS whatsapp-native
COPY --from=whatsmeow-build /out/anymsg-whatsmeow /usr/local/bin/anymsg-whatsmeow
COPY services/messaging/messaging_service ./messaging_service
"""
KEEP_GATEWAY_DOCKERFILE = """FROM python:3.13-slim@sha256:{base} AS build
COPY services/gateway/src ./src
RUN pip install ./src
FROM python:3.13-slim@sha256:{base}
COPY --from=build /app/.venv /app/.venv
"""
KEEP_SERVICES = {"gateway": "gateway", "telegram": "runtime", "whatsapp1": "whatsapp-native", "whatsapp2": "whatsapp-native"}
KEEP_COLLECTORS = ("telegram", "whatsapp1", "whatsapp2")


def keep_files():
    return {"services/messaging/Dockerfile": ("100644", KEEP_MESSAGING_DOCKERFILE.format(base="a" * 64, go="b" * 64)),
            "services/gateway/Dockerfile": ("100644", KEEP_GATEWAY_DOCKERFILE.format(base="a" * 64)),
            ".dockerignore": ("100644", ".git\n"),
            "services/messaging/requirements.lock": ("100644", "synthetic==1\n"),
            "services/messaging/messaging_service/app.py": ("100644", "APP = 1\n"),
            "services/messaging/messaging_service/migrations/001_synthetic.sql": ("100644", "-- synthetic\n"),
            "whatsapp/whatsmeow/main.go": ("100644", "package main\n"),
            "services/gateway/src/agent_gateway/app.py": ("100644", "GATEWAY = 1\n"),
            "docs/synthetic.md": ("100644", "notes\n")}


class KeepHost(Host):
    """`Host` with a gateway and three collectors on one project (ANYM-895): one container per service, a synthetic
    checkout per release (`files`, path to (mode, content)) and Compose labels that reach the container as Docker's do."""

    def __init__(self, tmp_path, monkeypatch):
        super().__init__(tmp_path, monkeypatch)
        self.units = {name: {"image": "old", "build": {"context": "/unused"}, "environment": {"SYNTHETIC": name}}
                      for name in KEEP_SERVICES}
        self.write_compose()
        self.project["services"] = dict(KEEP_SERVICES)
        self.containers = {name: {"Id": name + "0", "Image": OLD_IMAGE, "State": {"Running": True}, "Config": {"Labels": {}}}
                           for name in KEEP_SERVICES}
        self.files, self.order = keep_files(), []
        monkeypatch.setattr(p, "boot_id", lambda: "synthetic-boot")
        # One synthetic CI image per revision serves every target here; the per-target build labels are tested above.
        monkeypatch.setattr(p, "names_revision", lambda image, sha, dockerfile, recipe=None: True)

    def write_compose(self):
        self.original.write_text(json.dumps({"name": "synthetic", "services": {**self.units, "db": {"image": "postgres:pinned"}}}))

    def by_id(self, identity):
        return next(container for container in self.containers.values() if container["Id"] == identity)

    def run(self, argv, **kwargs):
        if argv[0] == "git" and "checkout" in argv:
            self.calls.append(argv)
            for path, (_, content) in self.files.items():
                (Path(argv[2]) / path).parent.mkdir(parents=True, exist_ok=True)
                (Path(argv[2]) / path).write_text(content)
            return ""
        if argv[0] == "git" and argv[3:] == ["ls-tree", "-r", "-z", "--full-tree", "HEAD"]:
            self.calls.append(argv)
            return "".join(f"{mode} blob {hashlib.sha1(content.encode()).hexdigest()}\t{path}\0"
                           for path, (mode, content) in sorted(self.files.items()))
        if argv[:2] == ["docker", "compose"] and any(word in argv for word in ("stop", "up", "ps")):
            self.calls.append(argv)
            if "stop" in argv:
                for service in argv[argv.index("stop") + 1:]:
                    self.order.append(("stop", service))
                    self.containers[service]["State"] = {"Running": False}
            elif "up" in argv:
                units = json.loads(Path(argv[argv.index("-f") + 1]).read_text())["services"]
                for service in argv[argv.index("180") + 1:]:
                    self.serial += 1
                    self.containers[service] = {"Id": f"{service}{self.serial}", "Image": units[service]["image"],
                                                "State": {"Running": True}, "Config": {"Labels": dict(units[service].get("labels") or {})}}
            else:
                return self.containers[argv[-1]]["Id"] + "\n"
            return ""
        if argv[:2] == ["docker", "ps"]:
            self.calls.append(argv)
            service = next(word.partition("service=")[2] for word in argv if word.startswith("label=com.docker.compose.service="))
            container = self.containers[service]
            return container["Id"] + "\n" if "--all" in argv or container["State"].get("Running") else ""
        if argv[:2] == ["docker", "inspect"]:
            self.calls.append(argv)
            container = self.by_id(argv[-1])
            if "--format" in argv:
                state = "running" if container["State"].get("Running") else "exited"
                return f"{state} {container['Config']['Labels'].get(p.REVISION_LABEL, '')}\n"
            return json.dumps([container])
        return super().run(argv, **kwargs)

    def ids(self):
        return {service: container["Id"] for service, container in self.containers.items()}

    def stopped(self):
        return [service for action, service in self.order if action == "stop"]


def keep_outcomes(result):
    return {service: next(iter(value)) for service, value in result["services"]["synthetic"].items()}


# spec: ANYM-895
def test_a_gateway_only_release_stops_and_ups_only_the_gateway_and_the_collectors_keep_their_containers(tmp_path, monkeypatch):
    host = KeepHost(tmp_path, monkeypatch)
    first = host.release(RELEASES[0])
    # Containers created before the rule carry no input label: the first release recreates everything, as today.
    assert keep_outcomes(first) == dict.fromkeys(KEEP_SERVICES, "recreated")
    assert all(value == {"recreated": ["unlabeled"]} for value in first["services"]["synthetic"].values())
    before, host.order = host.ids(), []
    host.files["services/gateway/src/agent_gateway/app.py"] = ("100644", "GATEWAY = 2\n")
    host.files["docs/synthetic.md"] = ("100644", "more notes\n")
    second = host.release(RELEASES[1])
    assert host.stopped() == ["gateway"]
    ups = [call for call in host.calls if call[:2] == ["docker", "compose"] and "up" in call]
    assert ups[-1][ups[-1].index("180") + 1:] == ["gateway"] and "--no-deps" in ups[-1]
    assert {service: host.ids()[service] for service in KEEP_COLLECTORS} == {service: before[service] for service in KEEP_COLLECTORS}
    assert host.ids()["gateway"] != before["gateway"]
    # Receipt (criterion 6): the changed parts of what was recreated; container and release of what was kept.
    assert second["services"]["synthetic"]["gateway"] == {"recreated": ["code"]}
    for service in KEEP_COLLECTORS:
        assert second["services"]["synthetic"][service] == {"kept": {"container": before[service], "revision": RELEASES[0]}}
    assert second["boot_id"] == "synthetic-boot"
    assert not re.search(r"GATEWAY|SYNTHETIC|notes", json.dumps(second))  # digests only, never content or values
    [project] = second["rollback"]["projects"]
    assert project["kept"] == dict.fromkeys(KEEP_COLLECTORS, RELEASES[0])
    # A third release that changes nothing keeps every service and names the release whose digest each one carries.
    host.order = []
    third = host.release(RELEASES[2])
    assert host.stopped() == [] and keep_outcomes(third) == dict.fromkeys(KEEP_SERVICES, "kept")
    assert third["services"]["synthetic"]["gateway"]["kept"]["revision"] == RELEASES[1]
    assert third["services"]["synthetic"]["telegram"]["kept"]["revision"] == RELEASES[0]


# spec: ANYM-895
def test_a_messaging_service_change_recreates_telegram_and_both_whatsapp_services(tmp_path, monkeypatch):
    host = KeepHost(tmp_path, monkeypatch)
    host.release(RELEASES[0])
    host.order = []
    host.files["services/messaging/messaging_service/app.py"] = ("100644", "APP = 2\n")
    result = host.release(RELEASES[1])
    assert sorted(host.stopped()) == sorted(KEEP_COLLECTORS)
    assert keep_outcomes(result) == {"gateway": "kept", **dict.fromkeys(KEEP_COLLECTORS, "recreated")}
    assert result["services"]["synthetic"]["telegram"] == {"recreated": ["code"]}


# spec: ANYM-895
def test_a_release_that_adds_a_messaging_migration_recreates_every_service_that_copies_the_package(tmp_path, monkeypatch):
    host = KeepHost(tmp_path, monkeypatch)
    host.release(RELEASES[0])
    host.files["services/messaging/messaging_service/migrations/002_synthetic.sql"] = ("100644", "-- next\n")
    result = host.release(RELEASES[1])
    for service in KEEP_COLLECTORS:
        assert result["services"]["synthetic"][service] == {"recreated": ["code", "migration"]}
    assert keep_outcomes(result)["gateway"] == "kept"


# spec: ANYM-895
@pytest.mark.parametrize("doubt", ["label", "restarting", "settings", "receipt"])
def test_a_container_with_any_doubt_about_its_inputs_is_recreated(tmp_path, monkeypatch, doubt):
    host = KeepHost(tmp_path, monkeypatch)
    host.release(RELEASES[0])
    telegram = host.containers["telegram"]
    if doubt == "label":  # a container created by hand with a copied label matches no receipt
        host.containers["telegram"] = {**copy.deepcopy(telegram), "Id": "telegram-by-hand"}
    elif doubt == "restarting":
        telegram["State"] = {"Running": True, "Restarting": True}
    elif doubt == "settings":
        telegram["HostConfig"] = {"Memory": 1}
    else:  # the receipt does not name the container's digest
        current = p.read(host.state / "current.json")
        current["rollback"]["projects"][0]["inputs"]["telegram"]["digest"] = "i1:" + "0" * 64
        p.write(host.state / "current.json", current)
    host.order = []
    result = host.release(RELEASES[1])
    assert host.stopped() == ["telegram"]
    assert keep_outcomes(result) == {**dict.fromkeys(KEEP_SERVICES, "kept"), "telegram": "recreated"}


# spec: ANYM-895
@pytest.mark.parametrize("change", ["copied file", "file mode", "run line", "dockerignore", "base pin", "build argument",
                                    "env file", "mounted file", "secret file", "definition", "dockerfile ignore",
                                    "configuration directory", "secret source"])
def test_the_input_digest_changes_with_each_input_and_not_with_docs_or_another_target(tmp_path, monkeypatch, change):
    files = keep_files()
    env, mounted, secret, twin = (tmp_path / name for name in ("api.env", "mounted.conf", "secret.txt", "twin.txt"))
    configuration, data = tmp_path / "configuration", tmp_path / "data"
    for directory in (configuration, data):
        directory.mkdir()
    for path in (env, mounted, secret, twin, configuration / "config.json"):
        path.write_text("synthetic\n")
    document = {"services": {"telegram": {"environment": {"SYNTHETIC": "1"}, "env_file": [str(env)],
                                          "volumes": [{"type": "bind", "source": str(mounted), "target": "/etc/x.conf"},
                                                      {"type": "bind", "source": str(data), "target": "/data"},
                                                      {"type": "bind", "source": str(configuration),
                                                       "target": "/run/deedkit/configuration", "read_only": True}],
                                          "secrets": [{"source": "token"}]}},
                "secrets": {"token": {"file": str(secret)}}}

    def inputs():
        source = tmp_path / "source"
        for path, (_, content) in files.items():
            (source / path).parent.mkdir(parents=True, exist_ok=True)
            (source / path).write_text(content)
        entries = {path: mode + " " + hashlib.sha1(content.encode()).hexdigest() for path, (mode, content) in files.items()}
        return p.service_inputs(document, "telegram", p.build_inputs(source, "runtime", entries))

    original = inputs()
    assert original["digest"].startswith(p.INPUT_VERSION)
    # Docs, the gateway's code and another target's (whatsmeow) files are not inputs of `runtime`.
    files["docs/synthetic.md"] = ("100644", "changed\n")
    files["services/gateway/src/agent_gateway/app.py"] = ("100644", "GATEWAY = 9\n")
    files["whatsapp/whatsmeow/main.go"] = ("100644", "package changed\n")
    (data / "data.bin").write_text("a writable mounted directory's content is data, not an input\n")
    assert inputs() == original
    dockerfile = files["services/messaging/Dockerfile"][1]
    if change == "copied file":
        files["services/messaging/messaging_service/app.py"] = ("100644", "APP = 1 \n")
    elif change == "file mode":
        files["services/messaging/requirements.lock"] = ("100755", files["services/messaging/requirements.lock"][1])
    elif change == "run line":
        files["services/messaging/Dockerfile"] = ("100644", dockerfile.replace("pip install", "pip install --no-cache"))
    elif change == "dockerignore":
        files[".dockerignore"] = ("100644", ".git\n*.pyc\n")
    elif change == "base pin":
        files["services/messaging/Dockerfile"] = ("100644", dockerfile.replace("a" * 64, "c" * 64))
    elif change == "build argument":
        recipe = p.build_recipe
        monkeypatch.setattr(p, "build_recipe", lambda target, sha: ({**recipe(target, sha)[0], "EXTRA": "1"}, ""))
    elif change == "env file":
        env.write_text("synthetic2\n")
    elif change == "mounted file":
        mounted.write_text("synthetic2\n")
    elif change == "secret file":
        secret.write_text("synthetic2\n")
    elif change == "dockerfile ignore":  # takes precedence over the context's .dockerignore
        files["services/messaging/Dockerfile.dockerignore"] = ("100644", "**/migrations\n")
    elif change == "configuration directory":
        (configuration / "config.json").write_text("synthetic2\n")
    elif change == "secret source":  # another file with the same bytes
        document["secrets"]["token"]["file"] = str(twin)
    else:
        document["services"]["telegram"]["environment"]["SYNTHETIC"] = "2"
    changed = inputs()
    assert changed["digest"] != original["digest"]
    part = {"copied file": "code", "file mode": "code", "run line": "dockerfile", "dockerignore": "dockerfile",
            "base pin": "dockerfile", "build argument": "recipe", "env file": "env", "mounted file": "mounts",
            "secret file": "secrets", "definition": "compose", "dockerfile ignore": "dockerfile",
            "configuration directory": "mounts", "secret source": "secrets"}[change]
    assert [name for name in p.INPUT_PARTS if changed["parts"][name] != original["parts"][name]] == [part]


def dockerfile_copy_sources(text, target):
    """An independent reading of what a target copies from the build context, sharing no code with production.py:
    stages split at FROM lines, the closure over FROM <stage> and COPY --from=<name or index>, and the source words
    of every COPY or ADD without --from in it."""
    joined = re.sub(r"\\\n", " ", "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#")))
    stages = []
    for line in joined.splitlines():
        words = line.split()
        if not words:
            continue
        if words[0].upper() == "FROM":
            words = [word for word in words[1:] if not word.startswith("--platform")]
            stages.append({"name": words[2].lower() if len(words) == 3 else None, "base": words[0].lower(),
                           "needs": [], "sources": set()})
        elif words[0].upper() == "RUN" and stages:
            stages[-1]["needs"] += [option.split("from=", 1)[1].split(",")[0].lower() for option in words
                                    if option.startswith("--mount=") and "from=" in option]
        elif words[0].upper() in ("COPY", "ADD") and stages:
            origin = [word.split("=", 1)[1].lower() for word in words if word.startswith("--from=")]
            if origin:
                stages[-1]["needs"].append(origin[0])
            else:
                stages[-1]["sources"].update(word for word in words[1:-1] if not word.startswith("--"))
    def find(name):
        return next((stage for index, stage in enumerate(stages) if name in (stage["name"], str(index))), None)
    pending, seen, sources = [find(target) if target else stages[-1]], [], set()
    while pending:
        stage = pending.pop()
        if stage is None or any(stage is item for item in seen):
            continue
        seen.append(stage)
        sources |= stage["sources"]
        pending += [find(stage["base"]) if stage["base"] in {item["name"] for item in stages} else None]
        pending += [find(name) for name in stage["needs"]]
    return sources


# reads: services/messaging/Dockerfile
# reads: services/gateway/Dockerfile
# reads: services/gateway/Dockerfile.dockerignore
# reads: services/gateway/.dockerignore
# reads: services/calendar/Dockerfile
# reads: services/calendar/.dockerignore
# reads: services/recordings/Dockerfile
# reads: services/recordings/.dockerignore
# reads: services/calls/Dockerfile
# reads: services/calls/.dockerignore
# reads: services/release-router/Dockerfile
# reads: .dockerignore
# spec: ANYM-895
def test_every_build_target_stage_chain_is_covered_by_the_input_digest():
    entries = p.tree_entries(ROOT)
    unreadable = {}
    for target, (context, dockerfile, stage, _) in p.BUILDS.items():
        text = (ROOT / dockerfile).read_text()
        try:
            stages = p.dockerfile_stages(text)
            chain = p.stage_chain(stages, stage)
            built = p.build_inputs(ROOT, target, entries)
        except p.Unreadable as exc:
            unreadable[target] = str(exc)
            continue
        assert built["code"] and built["dockerfile"] and built["recipe"]
        copied = {path for item in chain for path in item["copies"]}
        assert copied == dockerfile_copy_sources(text, stage), target
        selected = p.selected_entries(entries, p.context_paths(context, sorted(copied)))
        # Every copied path is in the release commit, and the digest holds nothing no stage copies.
        assert "missing" not in selected.values(), (target, sorted(path for path, value in selected.items() if value == "missing"))
        prefixes = p.context_paths(context, sorted(copied))
        assert all(any(not prefix or path == prefix or path.startswith(prefix + "/") or fnmatch.fnmatchcase(path, prefix)
                       for prefix in prefixes) for path in selected), target
        if stage in ("runtime", "whatsapp-native"):
            assert any(path.startswith(p.MESSAGING_PACKAGE + "/migrations/") for path in selected) and built["migration"]
    # A Dockerfile the parser cannot read is named here and its services are always recreated (fail-safe).
    assert unreadable == {}, unreadable
    document = {"services": {"api": {"image": "x"}}}
    assert p.service_inputs(document, "api", None)["digest"] is None


# spec: ANYM-895
@pytest.mark.parametrize("instruction", ["ONBUILD RUN true", "ADD https://example.invalid/x.tgz /x",
                                         "RUN --mount=type=bind,source=.,target=/src true", "ADD vendor.tar.gz /x",
                                         "RUN --mount=source=.,target=/src true", "RUN <<EOF\ntrue\nEOF",
                                         "COPY <<EOF /x\nCOPY a b\nEOF", 'COPY "src file.py" /app/',
                                         "COPY src --chmod=644 /app/", "RUN <<1\nFROM scratch\n1",
                                         "ADD git@example.invalid:x.git /x", "RUN\t<<1 cat\nFROM scratch\n1",
                                         'RUN --mount=type=bind,from="one",target=/m true', "RUN\ttrue",
                                         "COPY a /b", "RUN true\r", "MAINTAINER synthetic", "COPY --from='one' /a /b"])
def test_a_dockerfile_instruction_the_digest_cannot_place_refuses_the_keep(instruction):
    with pytest.raises(p.Unreadable):
        p.dockerfile_stages("FROM python:3.13-slim AS one\n" + instruction + "\n")


# spec: ANYM-895
@pytest.mark.parametrize("directive", ["# syntax=docker/dockerfile:1", "# escape=`", "#check=skip=all"])
def test_a_dockerfile_parser_directive_refuses_the_keep_and_a_quoted_run_argument_does_not(directive):
    with pytest.raises(p.Unreadable):
        p.dockerfile_stages(directive + "\nFROM python:3.13-slim AS one\nCOPY a /a\n")
    # Quotes in the command a RUN runs are the command's own, not Docker's options.
    stages = p.dockerfile_stages("# a comment, not a directive\nFROM python:3.13-slim AS one\n"
                                 'RUN pip install --index-url="https://example.invalid/simple" x\nCOPY a /a\n')
    assert stages[0]["copies"] == ["a"]


# spec: ANYM-895
def test_the_stage_chain_reads_options_before_json_index_references_and_any_case():
    text = ('FROM python:3.13-slim AS Build\nCOPY --chmod=644 ["src/file.py", "lib", "/app/"]\n'
            "FROM python:3.13-slim\nCOPY tools /tools\n"
            "FROM python:3.13-slim AS mounted\nCOPY vendor /vendor\n"
            "FROM python:3.13-slim\nCOPY --from=0 /app /app\nCOPY --from=1 /tools /tools\nCOPY --from=BUILD /x /x\n"
            "RUN --mount=type=bind,from=Mounted,target=/m true\n")
    chain = p.stage_chain(p.dockerfile_stages(text), "")
    assert sorted(path for stage in chain for path in stage["copies"]) == ["lib", "src/file.py", "tools", "vendor"]
    assert sorted(dockerfile_copy_sources(text.replace('--chmod=644 ["src/file.py", "lib", "/app/"]', "src/file.py lib /app/"),
                                          "")) == ["lib", "src/file.py", "tools", "vendor"]


# spec: ANYM-895
def test_a_configuration_directory_digest_covers_every_entry_within_its_bounds(tmp_path, monkeypatch):
    configuration = tmp_path / "configuration"
    (configuration / "nested").mkdir(parents=True)
    (configuration / "nested" / "policy.json").write_text("{}\n")
    first = p.input_directory_digest(configuration)
    (configuration / "nested").chmod(0o750)
    assert p.input_directory_digest(configuration) != first  # a directory mode is an input
    # A source mounted read-only and writable keeps the read-only mount's content digest.
    unit = {"volumes": [{"type": "bind", "source": str(configuration), "target": "/run/deedkit/a", "read_only": True},
                        {"type": "bind", "source": str(configuration), "target": "/data/b"}]}
    before = p.host_inputs({"services": {"api": unit}}, "api")
    (configuration / "nested" / "policy.json").write_text('{"changed": true}\n')
    assert p.host_inputs({"services": {"api": unit}}, "api")["mounts"] != before["mounts"]
    monkeypatch.setattr(p, "INPUT_DIRECTORY_FILES", 3)
    for index in range(3):
        (configuration / f"link{index}").symlink_to("nested")
    with pytest.raises(p.Unreadable):
        p.input_directory_digest(configuration)


# spec: ANYM-895
def test_a_glob_selects_directories_with_their_descendants_and_git_paths_are_never_quoted(tmp_path):
    entries = {"dir1/app.py": "100644 1", "dir2/deep/x.py": "100644 2", "dir3/y.py": "100644 3"}
    assert sorted(p.selected_entries(entries, ["dir[12]"])) == ["dir1/app.py", "dir2/deep/x.py"]
    repository = tmp_path / "repository"
    (repository / "services" / "messaging").mkdir(parents=True)
    (repository / "services" / "messaging" / "café \"tab\t.py").write_text("x = 1\n")
    git = ["git", "-C", str(repository), "-c", "user.name=synthetic", "-c", "user.email=synthetic@example.invalid"]
    for argv in (["init", "--quiet"], ["add", "."], ["commit", "--quiet", "-m", "synthetic"]):
        subprocess.run(git + argv, check=True, capture_output=True)
    first = p.tree_entries(repository)
    assert list(first) == ["services/messaging/café \"tab\t.py"]
    (repository / "services" / "messaging" / "café \"tab\t.py").write_text("x = 2\n")
    subprocess.run(git + ["commit", "--quiet", "-am", "change"], check=True, capture_output=True)
    second = p.tree_entries(repository)
    assert p.digest(p.selected_entries(second, ["services/messaging"])) != p.digest(p.selected_entries(first, ["services/messaging"]))


# spec: ANYM-895
def test_rollback_of_a_release_never_stops_or_recreates_a_service_it_kept(tmp_path, monkeypatch):
    host = KeepHost(tmp_path, monkeypatch)
    host.release(RELEASES[0])
    host.files["services/gateway/src/agent_gateway/app.py"] = ("100644", "GATEWAY = 2\n")
    host.release(RELEASES[1])
    collectors = {service: host.ids()[service] for service in KEEP_COLLECTORS}
    host.order = []
    outcome = p.rollback(host.settings, RELEASES[1])
    assert host.stopped() == ["gateway"]
    assert {service: host.ids()[service] for service in KEEP_COLLECTORS} == collectors
    assert host.containers["gateway"]["Image"] == host.image(RELEASES[0])
    assert outcome["previous_images"] == {"synthetic": {"gateway": host.image(RELEASES[0])}}
    # Image retention keeps what the rollback left running.
    assert outcome["kept_images"] == {"synthetic": dict.fromkeys(KEEP_COLLECTORS, host.image(RELEASES[0]))}
    reasons = []
    p.state_images(host.state, lambda image, why: reasons.append((image, why)), {}, label="", idle=True)
    assert (host.image(RELEASES[0]), "kept running through the rollback of " + RELEASES[1]) in reasons


# spec: ANYM-895
@pytest.mark.parametrize("state", [{"Running": False, "Status": "exited"}, {"Running": True, "Restarting": True},
                                   {"Running": True, "Health": {"Status": "unhealthy"}}],
                         ids=["stopped", "restarting", "unhealthy"])
def test_a_kept_container_that_fails_during_apply_fails_the_release(tmp_path, monkeypatch, state):
    host = KeepHost(tmp_path, monkeypatch)
    host.release(RELEASES[0])
    host.files["services/gateway/src/agent_gateway/app.py"] = ("100644", "GATEWAY = 2\n")
    host.main = RELEASES[1]
    p.prepare(host.settings, RELEASES[1])
    run = host.run

    def fails(argv, **kwargs):
        if argv[:2] == ["docker", "compose"] and "up" in argv:
            host.containers["telegram"]["State"] = dict(state)
        return run(argv, **kwargs)
    monkeypatch.setattr(p, "run", fails)
    with pytest.raises(p.Refused, match="health wait"):
        p.apply(host.settings, RELEASES[1])
    assert p.read(host.state / "current.json")["sha"] == RELEASES[0]
    assert p.read(host.state / RELEASES[1] / "result.json")["status"] == "unhealthy"


# spec: ANYM-895
@pytest.mark.parametrize("change", ["restarted", "paused", "unhealthy"])
def test_a_kept_container_that_fails_during_the_settle_fails_the_release(tmp_path, monkeypatch, change):
    host = KeepHost(tmp_path, monkeypatch)
    host.release(RELEASES[0])
    host.files["services/gateway/src/agent_gateway/app.py"] = ("100644", "GATEWAY = 2\n")
    host.main = RELEASES[1]
    p.prepare(host.settings, RELEASES[1])
    monkeypatch.setattr(p, "APPLY_SETTLE_SECONDS", 1)
    changes = {"restarted": {"RestartCount": 1}, "paused": {"State": {"Running": True, "Paused": True}},
               "unhealthy": {"State": {"Running": True, "Health": {"Status": "unhealthy"}}}}
    monkeypatch.setattr(p.time, "sleep", lambda seconds: host.containers["whatsapp2"].update(changes[change]))
    # A paused container is not a state rollback restores from (snapshot): the attempt stays unresolved for inspection.
    with pytest.raises(p.Refused, match="inspect it before any recovery" if change == "paused" else "health wait"):
        p.apply(host.settings, RELEASES[1])
    assert p.read(host.state / "current.json")["sha"] == RELEASES[0]
    assert not (host.state / RELEASES[1] / "result.json").exists() if change == "paused" else (
        p.read(host.state / RELEASES[1] / "result.json")["status"] == "unhealthy")


# spec: ANYM-895
@pytest.mark.parametrize("path", ["mounted file", "kept restarting", "during the call wait"])
def test_apply_refuses_before_any_stop_when_an_input_changed_since_prepare(tmp_path, monkeypatch, path):
    host = KeepHost(tmp_path, monkeypatch)
    mounted = tmp_path / "telegram.conf"
    mounted.write_text("synthetic\n")
    host.units["telegram"]["volumes"] = [{"type": "bind", "source": str(mounted), "target": "/data/synthetic.conf"}]
    host.write_compose()
    host.release(RELEASES[0])
    host.main = RELEASES[1]
    p.prepare(host.settings, RELEASES[1])
    if path == "mounted file":
        mounted.write_text("synthetic2\n")
    elif path == "kept restarting":
        host.containers["whatsapp1"]["State"] = {"Running": True, "Restarting": True}
    else:
        monkeypatch.setattr(p, "wait_for_calls", lambda settings, deadline: mounted.write_text("synthetic3\n"))
    host.order = []
    with pytest.raises(p.Refused, match="inputs_changed_since_prepare"):
        p.apply(host.settings, RELEASES[1])
    assert host.order == [] and not (host.state / RELEASES[1] / "attempted.json").exists()


# spec: ANYM-895
def test_apply_waits_for_calls_before_any_stop_when_it_keeps_services(tmp_path, monkeypatch):
    host = KeepHost(tmp_path, monkeypatch)
    host.release(RELEASES[0])
    host.files["services/gateway/src/agent_gateway/app.py"] = ("100644", "GATEWAY = 2\n")
    host.main = RELEASES[1]
    p.prepare(host.settings, RELEASES[1])
    monkeypatch.setattr(p, "wait_for_calls", lambda settings, deadline: host.order.append(("calls", None)))
    host.order = []
    p.apply(host.settings, RELEASES[1])
    assert host.order[:3] == [("calls", None), ("calls", None), ("stop", "gateway")]


# spec: ANYM-895
def test_status_accepts_a_kept_service_on_the_revision_its_digest_names(tmp_path, monkeypatch):
    host = KeepHost(tmp_path, monkeypatch)
    host.release(RELEASES[0])
    host.files["services/gateway/src/agent_gateway/app.py"] = ("100644", "GATEWAY = 2\n")
    current = host.release(RELEASES[1])
    items = {item["service"]: item for item in p.project_services(host.settings, current)}
    assert items["telegram"]["revision"] == RELEASES[0] and items["gateway"]["revision"] == RELEASES[1]
    assert not any("mark" in item for item in items.values())
    # A collector recreated by hand from an older release is still named.
    host.containers["whatsapp1"]["Config"]["Labels"][p.REVISION_LABEL] = "9" * 40
    items = {item["service"]: item for item in p.project_services(host.settings, current)}
    assert items["whatsapp1"]["mark"] == "differs from current.json"


class SessionHost(KeepHost):
    """`KeepHost` whose collectors bind their session directories and the canonical lock directory under
    deploy/private, as compose.collectors.yaml and compose.whatsapp.yaml do (ANYM-914). Every container carries the
    Compose labels and bind mounts Docker reports; `others` are containers outside the selected services."""

    def __init__(self, tmp_path, monkeypatch):
        super().__init__(tmp_path, monkeypatch)
        self.private, self.others = tmp_path / "deploy" / "private", []
        for service in KEEP_COLLECTORS:
            (self.private / f"{service}-session").mkdir(parents=True)
            self.units[service]["volumes"] = [
                {"type": "bind", "source": str(self.private / f"{service}-session"), "target": "/sessions"},
                {"type": "bind", "source": str(self.private / "session-locks"), "target": p.SESSION_LOCKS}]
        self.write_compose()
        monkeypatch.setattr(p, "SESSION_OWNER", (os.getuid(), os.getgid()))

    def other(self, identity, source, state=None, **labels):
        self.others.append({"Id": identity, "Name": "/" + identity, "Image": OLD_IMAGE, "State": state or {"Running": True},
                            "Config": {"Labels": labels}, "Mounts": [{"Type": "bind", "Source": str(source),
                                                                      "Destination": "/sessions"}]})

    def run(self, argv, **kwargs):
        for service, container in self.containers.items():
            container.setdefault("Config", {}).setdefault("Labels", {}).update({
                "com.docker.compose.project": "synthetic", "com.docker.compose.service": service,
                "com.docker.compose.oneoff": "False"})
            container.setdefault("Mounts", [{"Type": "bind", "Source": volume["source"], "Destination": volume["target"]}
                                            for volume in self.units[service].get("volumes", [])])
        everything = [*self.containers.values(), *self.others]
        if argv == ["docker", "ps", "--all", "--quiet", "--no-trunc"]:
            self.calls.append(argv)
            return "".join(container["Id"] + "\n" for container in everything)
        if argv[:2] == ["docker", "inspect"] and "--format" not in argv and (len(argv) > 3 or argv[2].startswith("other")):
            self.calls.append(argv)
            return json.dumps([next(item for item in everything if item["Id"] == identity) for identity in argv[2:]])
        return super().run(argv, **kwargs)


# spec: ANYM-914
def test_a_first_release_on_a_host_without_session_locks_creates_the_directory_before_any_stop_and_starts_the_collectors(
        tmp_path, monkeypatch):
    host = SessionHost(tmp_path, monkeypatch)
    locks, seen, run = host.private / "session-locks", [], host.run
    for unit in host.units.values():
        p.validate_runtime(unit)  # the mount sits inside the existing /run/deedkit root

    def observed(argv, **kwargs):
        if argv[:2] == ["docker", "compose"] and "stop" in argv:
            seen.append(locks.is_dir())
        return run(argv, **kwargs)
    monkeypatch.setattr(p, "run", observed)
    assert not os.path.lexists(locks)
    result = host.release(RELEASES[0])
    assert seen == [True] and sorted(host.stopped()) == sorted(KEEP_SERVICES)
    found = os.lstat(locks)
    assert stat.S_ISDIR(found.st_mode) and stat.S_IMODE(found.st_mode) == 0o700
    assert (found.st_uid, found.st_gid) == p.SESSION_OWNER
    assert all(host.containers[service]["State"]["Running"] and host.containers[service]["Image"] == host.image(RELEASES[0])
               for service in KEEP_COLLECTORS) and result["status"] == "applied"
    # A later release finds the directory and leaves it as it is.
    host.files["services/messaging/messaging_service/app.py"] = ("100644", "APP = 2\n")
    assert host.release(RELEASES[1])["status"] == "applied" and os.lstat(locks).st_ino == found.st_ino


# spec: ANYM-914
@pytest.mark.parametrize("case", ["group_readable", "symlink", "file"])
def test_an_existing_session_lock_path_that_is_not_a_private_directory_refuses_before_any_stop_and_stays_as_it_is(
        tmp_path, monkeypatch, case):
    host = SessionHost(tmp_path, monkeypatch)
    locks = host.private / "session-locks"
    if case == "group_readable":
        locks.mkdir(mode=0o700)
        locks.chmod(0o750)
    elif case == "symlink":
        (tmp_path / "elsewhere").mkdir(mode=0o700)
        locks.symlink_to(tmp_path / "elsewhere")
    else:
        locks.write_text("")
    before = os.lstat(locks)
    host.main = RELEASES[0]
    with pytest.raises(p.Refused, match="session_locks_unsafe"):
        p.prepare(host.settings, RELEASES[0])
    assert host.stopped() == [] and not (host.state / RELEASES[0] / "prepared.json").exists()
    assert (os.lstat(locks).st_mode, os.lstat(locks).st_uid) == (before.st_mode, before.st_uid)


# spec: ANYM-914
def test_a_session_lock_directory_opened_to_its_group_after_preparation_refuses_apply_before_any_stop(tmp_path, monkeypatch):
    host = SessionHost(tmp_path, monkeypatch)
    host.main = RELEASES[0]
    p.prepare(host.settings, RELEASES[0])
    (host.private / "session-locks").chmod(0o750)
    with pytest.raises(p.Refused, match="session_locks_unsafe"):
        p.apply(host.settings, RELEASES[0])
    assert host.stopped() == [] and not (host.state / RELEASES[0] / "attempted.json").exists()
    assert stat.S_IMODE(os.lstat(host.private / "session-locks").st_mode) == 0o750


# spec: ANYM-914
@pytest.mark.parametrize("case", ["pair_run", "paused_copy", "other_project", "restarting_native_copy"])
def test_a_container_that_is_not_the_compose_service_of_a_session_directory_refuses_the_release_before_any_stop(
        tmp_path, monkeypatch, case):
    host = SessionHost(tmp_path, monkeypatch)
    if case == "pair_run":  # `compose run` of telegram_pair on the service's own directory
        host.other("other-pair", host.private / "telegram-session", **{
            "com.docker.compose.project": "synthetic", "com.docker.compose.service": "telegram",
            "com.docker.compose.oneoff": "True"})
    elif case == "paused_copy":  # a legacy container on a copied session directory, paused
        (host.private / "telegram-session.copy").mkdir()
        (host.private / "telegram-session.copy" / "synthetic.session").write_text("")
        host.other("other-legacy", host.private / "telegram-session.copy", {"Running": True, "Paused": True})
    elif case == "other_project":
        host.other("other-project", host.private / "whatsapp1-session", **{
            "com.docker.compose.project": "legacy", "com.docker.compose.service": "whatsapp1"})
    else:  # outside Compose, on a copy whose store sits one directory down
        (host.private / "copy" / "whatsapp").mkdir(parents=True)
        (host.private / "copy" / "whatsapp" / "auth.sqlite").write_text("")
        host.other("other-native", host.private / "copy", {"Running": True, "Restarting": True})
    host.main = RELEASES[0]
    p.prepare(host.settings, RELEASES[0])
    with pytest.raises(p.Refused, match="foreign_session_opener: container " + host.others[0]["Id"]):
        p.apply(host.settings, RELEASES[0])
    assert host.stopped() == [] and not (host.state / RELEASES[0] / "attempted.json").exists()


# spec: ANYM-914
def test_the_session_services_and_containers_without_a_session_store_pass_the_opener_check(tmp_path, monkeypatch):
    host = SessionHost(tmp_path, monkeypatch)
    (host.private / "exports").mkdir()
    (host.private / "exports" / "notes.json").write_text("{}")
    host.other("other-exports", host.private / "exports")  # no session store
    host.other("other-pair", host.private / "telegram-session", {"Running": False, "Status": "exited"}, **{
        "com.docker.compose.project": "synthetic", "com.docker.compose.service": "telegram",
        "com.docker.compose.oneoff": "True"})  # an exited pair run opens nothing
    (host.private / "telegram-session" / "synthetic.session").write_text("")
    assert host.release(RELEASES[0])["status"] == "applied"
    assert ["docker", "ps", "--all", "--quiet", "--no-trunc"] in host.calls
    assert sorted(host.stopped()) == sorted(KEEP_SERVICES)


# spec: ANYM-914
@pytest.mark.parametrize("case", ["no_lock_mount", "short_grace", "installed"])
def test_a_claim_release_onto_host_compose_files_without_the_lock_mount_or_the_grace_refuses_before_any_stop(
        tmp_path, monkeypatch, case):
    host = SessionHost(tmp_path, monkeypatch)
    host.files[p.SESSION_CLAIM] = ("100644", "CLAIMS = 1\n")  # the release carries the claim
    for service in KEEP_COLLECTORS:
        unit = host.units[service]
        unit["stop_grace_period"] = "10s" if case == "short_grace" else "25s"
        if case == "no_lock_mount":  # the host's pre-claim Compose files: a release never edits them
            unit["volumes"] = [volume for volume in unit["volumes"] if volume["target"] != p.SESSION_LOCKS]
    host.write_compose()
    if case == "installed":
        assert host.release(RELEASES[0])["status"] == "applied"
        return
    host.main = RELEASES[0]
    with pytest.raises(p.Refused, match="session_locks_unmounted: session service"):
        p.prepare(host.settings, RELEASES[0])
    assert host.stopped() == [] and not (host.state / RELEASES[0] / "prepared.json").exists()


# spec: ANYM-914
@pytest.mark.parametrize("case", ["named_volume", "deep_copy", "too_large"])
def test_a_foreign_container_with_a_session_store_in_a_volume_deeper_down_or_unsearchable_refuses_the_release(
        tmp_path, monkeypatch, case):
    host = SessionHost(tmp_path, monkeypatch)
    monkeypatch.setattr(p, "SESSION_SCAN_ENTRIES", 8)
    if case == "named_volume":  # a legacy container whose named volume holds a copied session
        volume = tmp_path / "volumes" / "legacy_tg" / "_data"
        volume.mkdir(parents=True)
        (volume / "copied.session").write_text("")
        host.others.append({"Id": "other-volume", "Name": "/other-volume", "Image": OLD_IMAGE, "State": {"Running": True},
                            "Config": {"Labels": {}}, "Mounts": [{"Type": "volume", "Name": "legacy_tg",
                                                                  "Source": str(volume), "Destination": "/data"}]})
    elif case == "deep_copy":  # three levels down, past the two levels a configured service's mount is searched
        (host.private / "backup" / "2026" / "tg").mkdir(parents=True)
        (host.private / "backup" / "2026" / "tg" / "copied.session").write_text("")
        host.other("other-deep", host.private / "backup")
    else:  # more entries than the search may read: ownership cannot be established, so it refuses
        (host.private / "bulk").mkdir()
        for n in range(20):
            (host.private / "bulk" / f"{n}.bin").write_text("")
        host.other("other-bulk", host.private / "bulk")
    host.main = RELEASES[0]
    p.prepare(host.settings, RELEASES[0])
    with pytest.raises(p.Refused, match="foreign_session_opener: container " + host.others[0]["Id"]):
        p.apply(host.settings, RELEASES[0])
    assert host.stopped() == [] and not (host.state / RELEASES[0] / "attempted.json").exists()


# spec: ANYM-914
def test_a_large_foreign_mount_passes_when_exempted_and_a_configured_services_data_path_is_not_searched(
        tmp_path, monkeypatch):
    host = SessionHost(tmp_path, monkeypatch)
    monkeypatch.setattr(p, "SESSION_SCAN_ENTRIES", 8)
    for name in ("bulk", "files"):
        (host.private / name).mkdir()
        for n in range(20):
            (host.private / name / f"{n}.bin").write_text("")
    host.other("other-bulk", host.private / "bulk")
    host.other("other-files", host.private / "files")  # e.g. a file-lifecycle worker on the archive's files volume
    service = next(service for service in KEEP_SERVICES if service not in KEEP_COLLECTORS)
    host.units[service].setdefault("volumes", []).append(
        {"type": "bind", "source": str(host.private / "files"), "target": "/data"})
    host.write_compose()
    host.settings["session_scan_exempt"] = [str(host.private / "bulk")]
    assert host.release(RELEASES[0])["status"] == "applied"
    assert sorted(host.stopped()) == sorted(KEEP_SERVICES)


# spec: ANYM-914
def test_after_a_claim_release_ran_healthy_a_rollback_to_a_tree_without_claims_is_refused_unless_a_reason_is_given(
        tmp_path, monkeypatch):
    host = KeepHost(tmp_path, monkeypatch)
    host.release(RELEASES[0])
    assert not (host.state / p.CLAIM_RECORD).exists()
    host.files[p.SESSION_CLAIM] = ("100644", "CLAIM = 1\n")
    host.release(RELEASES[1])
    assert p.read(host.state / p.CLAIM_RECORD)["sha"] == RELEASES[1]
    host.order = []
    with pytest.raises(p.Refused, match="pre_claim_rollback_refused"):
        p.rollback(host.settings, RELEASES[1])
    assert host.order == [] and not (host.state / RELEASES[1] / "rollback-attempted.json").exists()
    outcome = p.rollback(host.settings, RELEASES[1], pre_claim="synthetic: one host runs sessions")
    assert outcome["pre_claim_rollback"] == "synthetic: one host runs sessions"
    assert p.read(host.state / RELEASES[1] / "rollback.json") == outcome
    assert sorted(host.stopped()) == sorted(KEEP_COLLECTORS)
    assert all(host.containers[service]["Image"] == host.image(RELEASES[0]) for service in KEEP_COLLECTORS)


# spec: ANYM-914
def test_a_rollback_between_two_claim_releases_needs_no_reason(tmp_path, monkeypatch):
    host = KeepHost(tmp_path, monkeypatch)
    host.files[p.SESSION_CLAIM] = ("100644", "CLAIM = 1\n")
    host.release(RELEASES[0])
    host.files["services/messaging/messaging_service/app.py"] = ("100644", "APP = 2\n")
    host.release(RELEASES[1])
    outcome = p.rollback(host.settings, RELEASES[1])
    assert outcome["status"] == "rolled-back" and "pre_claim_rollback" not in outcome


# spec: ANYM-914
def test_the_restore_of_a_release_that_failed_its_health_wait_needs_no_reason_after_claims_ran(tmp_path, monkeypatch):
    host = KeepHost(tmp_path, monkeypatch)
    host.release(RELEASES[0])  # its tree has no session_claim.py
    p.write(host.state / p.CLAIM_RECORD, {"sha": "9" * 40, "recorded_at": "2026-11-09T00:00:00Z"})
    host.files["services/messaging/messaging_service/app.py"] = ("100644", "APP = 2\n")
    host.main = RELEASES[1]
    p.prepare(host.settings, RELEASES[1])
    run = host.run

    def fails(argv, **kwargs):
        output = run(argv, **kwargs)
        if argv[:2] == ["docker", "compose"] and "up" in argv:
            raise p.Refused("docker failed (exit 1)")
        return output
    monkeypatch.setattr(p, "run", fails)
    with pytest.raises(p.Refused, match="health wait"):
        p.apply(host.settings, RELEASES[1])
    monkeypatch.setattr(p, "run", run)
    assert p.read(host.state / RELEASES[1] / "result.json")["status"] == "unhealthy"
    outcome = p.rollback(host.settings, RELEASES[1])
    assert outcome["status"] == "rolled-back" and "pre_claim_rollback" not in outcome
    assert all(host.containers[service]["Image"] == host.image(RELEASES[0]) for service in KEEP_COLLECTORS)


# ANYM-773: Compose as both tested versions behave. Default `config` inlines env files, prints `$` as `$$` and interpolates;
# `--no-interpolate` keeps absolute references (a path starting with a variable keeps it after the project directory).
ENV_CANARY = "synthetic-env-canary-773"


def env_file_values(path):
    values = {}
    if not os.path.exists(path):
        return values
    for line in Path(path).read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator:
            if value[:1] == '"':
                value = value[1:-1].replace('\\"', '"').replace("\\$", "$")
            elif value[:1] == "'":
                value = value[1:-1]
            values[key] = value
    return values


def render_compose(argv, text, directory, variables=None, ignores=False):
    document = json.loads(text)
    keep = "--no-interpolate" in argv and not ignores

    def expand(value):
        if isinstance(value, str):
            for name, replacement in (variables or {}).items():
                value = value.replace("${" + name + "}", replacement)
            return value
        if isinstance(value, list):
            return [expand(item) for item in value]
        return {key: expand(item) for key, item in value.items()} if isinstance(value, dict) else value

    if not keep:
        document = expand(document)
    for unit in document["services"].values():
        entries = [{"path": entry} if isinstance(entry, str) else entry for entry in unit.pop("env_file", None) or []]
        if keep:
            if entries:
                unit["env_file"] = [{**entry, "path": posixpath.join(directory, entry["path"]), "required": True} for entry in entries]
            continue
        environment = {}
        for entry in entries:
            environment.update({key: value.replace("$", "$$") for key, value in env_file_values(entry["path"]).items()})
        environment.update(unit.get("environment") or {})
        if environment:
            unit["environment"] = environment
    return json.dumps(document)


class EnvHost(Host):
    """`Host` whose `api` service names a synthetic env file."""

    def __init__(self, tmp_path, monkeypatch, content=f"SYNTHETIC_CANARY={ENV_CANARY}\n", **unit):
        super().__init__(tmp_path, monkeypatch)
        self.env = tmp_path / "api.env"
        self.env.write_text(content)
        document = json.loads(self.original.read_text())
        document["services"]["api"] = {**document["services"]["api"], "env_file": [str(self.env)], **unit}
        self.original.write_text(json.dumps(document))
        self.variables, self.ignores = {}, False
        monkeypatch.setattr(p, "run_input", lambda argv, data, **kw: render_compose(argv, data.decode(), str(tmp_path), self.variables))

    def run(self, argv, **kwargs):
        if argv[:2] == ["docker", "compose"] and "config" in argv:
            self.calls.append(argv)
            return render_compose(argv, Path(argv[argv.index("-f") + 1]).read_text(), str(self.root), self.variables, self.ignores)
        return super().run(argv, **kwargs)

    def canary_files(self, canary=ENV_CANARY):
        return [str(path) for path in self.state.rglob("*") if path.is_file() and canary.encode() in path.read_bytes()]


BASE_RELEASES = ROOT / "tests" / "fixtures" / "anym773_base"


def legacy_prepare(host, sha, monkeypatch=None):
    """A release as the base code left it: documents and receipt recorded by running that code's `prepare` over this same
    synthetic host (tests/fixtures/anym773_base/README.md). Only what binds the temporary directory is filled in here."""
    release, recorded = host.state / sha, BASE_RELEASES / sha
    release.mkdir(mode=0o700)
    for name in ("synthetic.json", "synthetic.before.json"):
        (release / name).write_bytes((recorded / name).read_bytes())
    receipt = json.loads((recorded / "prepared.json").read_text())
    for project in receipt["projects"]:
        project["file"] = str(release / (project["name"] + ".json"))
    current = host.state / "current.json"
    receipt["config_digest"] = p.settings_digest(host.settings)
    receipt["current_digest"] = p.digest(p.read(current) if current.exists() else None)
    p.write(release / "prepared.json", receipt)
    host.images[host.image(sha)] = ImageStore.image("c", revision=sha, source=sha) | {"Id": host.image(sha)}
    assert host.canary_files()  # the legacy shape holds the value inline
    return receipt


# spec: ANYM-773
def test_prepare_writes_env_file_references_and_no_value_with_digests_over_the_resolved_document(tmp_path, monkeypatch):
    host = EnvHost(tmp_path, monkeypatch)
    host.main = RELEASES[0]
    receipt = p.prepare(host.settings, RELEASES[0])
    assert host.canary_files() == []
    for name in ("synthetic.json", "synthetic.before.json"):
        unit = p.read(host.state / RELEASES[0] / name)["services"]["api"]
        assert unit["env_file"] == [{"path": str(host.env), "required": True}] and "SYNTHETIC_CANARY" not in unit["environment"]
    assert unit["environment"]["SYNTHETIC"] == "literal"
    resolved = json.loads(render_compose(["config"], host.original.read_text(), str(tmp_path)))
    assert receipt["render"] == 2 and receipt["projects"][0]["before_digest"] == p.digest(resolved)
    assert receipt["env_files"] == [{"path": str(host.env), "sha256": hashlib.sha256(host.env.read_bytes()).hexdigest()}]


# spec: ANYM-773
@pytest.mark.parametrize("line", ["LD_PRELOAD=/data/evil.so", "PYTHONPATH=/data/code"])
def test_an_import_override_in_an_env_file_still_refuses_preparation(tmp_path, monkeypatch, line):
    host = EnvHost(tmp_path, monkeypatch, content=line + "\n")
    host.main = RELEASES[0]
    with pytest.raises(p.Refused, match="runtime import path overrides"):
        p.prepare(host.settings, RELEASES[0])


# spec: ANYM-773
@pytest.mark.parametrize("failure", ["inlines", "drops", "fails"])
def test_a_compose_that_cannot_give_references_refuses_with_nothing_written(tmp_path, monkeypatch, failure):
    host = EnvHost(tmp_path, monkeypatch)
    host.main = RELEASES[0]
    real = host.run

    def broken(argv, **kwargs):
        if "deedkit-env-probe" in argv:
            if failure == "fails":
                raise p.Refused("docker failed (exit 1)")
            document = json.loads(Path(argv[argv.index("-f") + 1]).read_text())
            unit = document["services"]["probe"]
            environment = env_file_values(unit.pop("env_file")[0]["path"])
            if failure == "inlines":
                unit["environment"] = environment
            return json.dumps(document)
        return real(argv, **kwargs)

    monkeypatch.setattr(p, "run", broken)
    with pytest.raises(p.Refused, match="compose_ignores_env_resolution") as refused:
        p.prepare(host.settings, RELEASES[0])
    assert ENV_CANARY not in str(refused.value) and not (host.state / RELEASES[0]).exists()


# spec: ANYM-773
@pytest.mark.parametrize("canary", ['syn$thetic"canary-773', "plain-canary-value-773"])
def test_a_value_of_an_env_file_elsewhere_in_the_document_refuses_without_naming_it(tmp_path, monkeypatch, canary):
    quoted = "'" + canary + "'"
    host = EnvHost(tmp_path, monkeypatch, content=f"API_OTHER={quoted}\n", command=["--note", "${REFERENCE}"])
    host.variables = {"REFERENCE": canary.replace("$", "$$")}
    host.main = RELEASES[0]
    with pytest.raises(p.Refused, match="env_value_in_evidence") as refused:
        p.prepare(host.settings, RELEASES[0])
    message = str(refused.value)
    assert message == f"env_value_in_evidence: {host.env}" and "API_OTHER" not in message and canary not in message
    assert not list((host.state / RELEASES[0]).glob("*.json"))


# spec: ANYM-773
def test_a_literal_the_source_writes_at_that_place_passes_unless_its_name_looks_secret(tmp_path, monkeypatch):
    # Measured on the host: POSTGRES_USER and POSTGRES_DB of postgres.env stand in the db healthcheck as literals.
    probe = {"test": ["CMD", "pg_isready", "-U", "synthetic_owner"]}
    host = EnvHost(tmp_path, monkeypatch, content="POSTGRES_USER=synthetic_owner\n", healthcheck=probe)
    host.main = RELEASES[0]
    p.prepare(host.settings, RELEASES[0])
    (tmp_path / "second").mkdir()
    host = EnvHost(tmp_path / "second", monkeypatch, content="API_PASSWORD=synthetic_owner\n", healthcheck=probe)
    host.main = RELEASES[1]
    with pytest.raises(p.Refused, match="env_value_in_evidence"):
        p.prepare(host.settings, RELEASES[1])


# spec: ANYM-773
def test_a_default_in_the_source_is_not_a_literal_and_refuses(tmp_path, monkeypatch):
    host = EnvHost(tmp_path, monkeypatch, content="API_OTHER=synthetic-default-value\n", command=["--note", "${NOTE:-synthetic-default-value}"])
    host.variables = {"NOTE:-synthetic-default-value": "synthetic-default-value"}
    host.main = RELEASES[0]
    with pytest.raises(p.Refused, match="env_value_in_evidence"):
        p.prepare(host.settings, RELEASES[0])


# spec: ANYM-773
@pytest.mark.parametrize("key,value", [("API_CREDENTIAL", "10.99.0.2:8443"), ("DATABASE_URL", "10.99.0.2:8443"), ("VAULT_TOKEN", "/looks/like/a/path")])
def test_a_secret_named_interpolation_value_refuses_also_when_it_looks_like_a_path(tmp_path, monkeypatch, key, value):
    host = EnvHost(tmp_path, monkeypatch, content="UNRELATED=x\n", command=["--hosts", "${REFERENCE}"])
    host.variables = {"REFERENCE": value}
    (tmp_path / ".env").write_text(f"{key}={value}\n")
    host.main = RELEASES[0]
    with pytest.raises(p.Refused, match="env_value_in_evidence"):
        p.prepare(host.settings, RELEASES[0])


# spec: ANYM-773
def test_interpolation_files_refuse_only_a_secret_named_value_in_the_document(tmp_path, monkeypatch):
    host = EnvHost(tmp_path, monkeypatch, command=["--hosts", "${PUBLIC_ADDRESS}"])
    host.variables = {"PUBLIC_ADDRESS": "10.99.0.2:8443"}
    (tmp_path / ".env").write_text("PUBLIC_ADDRESS=10.99.0.2:8443\nCERT_TOKEN_FILE=/run/secrets/certificate\n")
    host.main = RELEASES[0]
    p.prepare(host.settings, RELEASES[0])  # the address a document needs; a path under a secret-looking name
    (tmp_path / ".env").write_text("PUBLIC_API_TOKEN=10.99.0.2:8443\n")
    host.main = RELEASES[1]
    with pytest.raises(p.Refused, match="env_value_in_evidence") as refused:
        p.prepare(host.settings, RELEASES[1])
    assert str(refused.value) == f"env_value_in_evidence: {tmp_path / '.env'}"


# spec: ANYM-773
@given(st.text(alphabet='a$"\\b', max_size=12))
def test_a_value_compose_printed_is_escaped_once_and_reads_back_as_printed(value):
    printed = value.replace("$", "$$")  # what `config` prints
    assert p.escaped_rendered(printed) == printed and p.escaped_rendered({"k": [printed]}) == {"k": [printed]}
    assert p.escaped(value) == printed  # a raw value: `router$$name` keeps both of its dollar signs


# spec: ANYM-773
def test_a_legacy_release_applies_and_rolls_back_and_its_files_stay_as_written(tmp_path, monkeypatch):
    host = EnvHost(tmp_path, monkeypatch)
    host.main = RELEASES[0]
    p.prepare(host.settings, RELEASES[0])
    p.apply(host.settings, RELEASES[0])
    host.main = RELEASES[1]
    legacy_prepare(host, RELEASES[1], monkeypatch)
    before = {path: path.read_bytes() for path in (host.state / RELEASES[1]).glob("synthetic*.json")}
    assert "render" not in p.read(host.state / RELEASES[1] / "prepared.json")
    p.apply(host.settings, RELEASES[1])
    p.rollback(host.settings, RELEASES[1])
    assert host.container["Image"] == host.image(RELEASES[0])
    assert before == {path: path.read_bytes() for path in before}


# spec: ANYM-773
def test_a_new_release_on_a_legacy_current_applies_and_rolls_back(tmp_path, monkeypatch):
    host = EnvHost(tmp_path, monkeypatch)
    host.main = RELEASES[0]
    legacy_prepare(host, RELEASES[0], monkeypatch)
    p.apply(host.settings, RELEASES[0])
    host.main = RELEASES[1]
    p.prepare(host.settings, RELEASES[1])
    assert p.read(host.state / RELEASES[1] / "prepared.json")["render"] == 2
    p.apply(host.settings, RELEASES[1])
    p.rollback(host.settings, RELEASES[1])
    assert host.container["Image"] == host.image(RELEASES[0])
    assert p.read(host.state / "current.json")["status"] == "rolled-back"


# spec: ANYM-773
def test_a_legacy_release_keeps_its_unhealthy_rollback(tmp_path, monkeypatch):
    host = EnvHost(tmp_path, monkeypatch)
    host.main = RELEASES[0]
    p.prepare(host.settings, RELEASES[0])
    p.apply(host.settings, RELEASES[0])
    host.main = RELEASES[1]
    legacy_prepare(host, RELEASES[1], monkeypatch)
    run = host.run

    def failing(argv, **kwargs):
        if "up" in argv and "--wait" in argv:
            run(argv, **kwargs)
            raise p.Refused("docker failed (exit 1)")
        return run(argv, **kwargs)

    monkeypatch.setattr(p, "run", failing)
    with pytest.raises(p.Refused, match="failed its health wait"):
        p.apply(host.settings, RELEASES[1])
    assert p.read(host.state / RELEASES[1] / "result.json")["status"] == "unhealthy"
    monkeypatch.setattr(p, "run", run)
    p.rollback(host.settings, RELEASES[1])
    assert host.container["Image"] == host.image(RELEASES[0])


# spec: ANYM-773
def test_an_interrupted_legacy_apply_refuses_and_its_rollback_target_restores_by_the_manual_procedure(tmp_path, monkeypatch):
    class Crash(BaseException):
        pass

    host = EnvHost(tmp_path, monkeypatch)
    host.main = RELEASES[0]
    p.prepare(host.settings, RELEASES[0])
    p.apply(host.settings, RELEASES[0])
    host.main = RELEASES[1]
    prepared = legacy_prepare(host, RELEASES[1], monkeypatch)
    run = host.run

    def crashing(argv, **kwargs):
        if "up" in argv and "--wait" in argv:
            raise Crash
        return run(argv, **kwargs)

    monkeypatch.setattr(p, "run", crashing)
    with pytest.raises(Crash):
        p.apply(host.settings, RELEASES[1])
    monkeypatch.setattr(p, "run", run)
    assert not host.container["State"]["Running"]  # stopped, nothing started: the partial state
    assert RELEASES[1] in p.unfinished(host.state)
    with pytest.raises(p.Refused, match="already attempted"):
        p.apply(host.settings, RELEASES[1])
    with pytest.raises(p.Refused, match="unfinished"):
        p.rollback(host.settings, RELEASES[0])
    # The tool has no automatic recovery. The manual procedure is `up` of the bound before-release file.
    before = host.state / RELEASES[1] / "synthetic.before.json"
    assert p.digest(p.read(before)) == prepared["projects"][0]["before_file_digest"]
    command = ["docker", "compose", "--profile", "*", "--project-name", "synthetic", "-f", str(before)]
    run(command + ["up", "-d", "--no-build", "--pull", "never", "--no-deps", "--wait", "api"])
    assert host.container["Image"] == host.image(RELEASES[0]) and host.container["State"]["Running"]
    assert p.read(before)["services"]["api"]["environment"]["SYNTHETIC_CANARY"] == ENV_CANARY


# spec: ANYM-773
@pytest.mark.parametrize("change", ["comment", "value"])
def test_a_changed_env_file_of_a_selected_service_refuses_apply(tmp_path, monkeypatch, change):
    host = EnvHost(tmp_path, monkeypatch)
    host.main = RELEASES[0]
    p.prepare(host.settings, RELEASES[0])
    host.env.write_text(host.env.read_text() + ("# a comment\n" if change == "comment" else "API_OTHER=changed-value\n"))
    host.calls.clear()
    with pytest.raises(p.Refused, match="production or prepared configuration changed since preparation"):
        p.apply(host.settings, RELEASES[0])
    assert host.mutations() == []


# spec: ANYM-773
@pytest.mark.parametrize("legacy", [False, True])
def test_rollback_after_an_env_file_changed_refuses_and_with_it_unchanged_succeeds(tmp_path, monkeypatch, legacy):
    host = EnvHost(tmp_path, monkeypatch)
    host.main = RELEASES[0]
    p.prepare(host.settings, RELEASES[0])
    p.apply(host.settings, RELEASES[0])
    host.main = RELEASES[1]
    legacy_prepare(host, RELEASES[1], monkeypatch) if legacy else p.prepare(host.settings, RELEASES[1])
    p.apply(host.settings, RELEASES[1])
    content = host.env.read_text()
    host.env.write_text(content + "API_OTHER=changed-value\n")
    host.calls.clear()
    with pytest.raises(p.Refused, match="production changed since this release was applied"):
        p.rollback(host.settings, RELEASES[1])
    assert host.mutations() == []
    host.env.write_text(content)
    p.rollback(host.settings, RELEASES[1])
    assert host.container["Image"] == host.image(RELEASES[0])


# spec: ANYM-773
def test_real_compose_keeps_references_and_reads_the_written_file_back_exactly(tmp_path, monkeypatch):
    if not shutil.which("docker") or subprocess.run(["docker", "compose", "version"], capture_output=True, timeout=15).returncode:
        pytest.skip("Docker Compose CLI is needed for the real parser")
    canary, dollar = "synthetic-real-canary", "syn$thetic-dollar-value"
    (tmp_path / "state" / "service").mkdir(parents=True)
    (tmp_path / "app.env").write_text(f"APP_CREDENTIAL={canary}\nAPP_DOLLAR='{dollar}'\nOVERRIDDEN=from-the-env-file\nPOSTGRES_USER=synthetic_owner\n")
    (tmp_path / "state" / "service" / "s.env").write_text(f"STATE_CREDENTIAL={canary}-state\n")
    (tmp_path / "interp.env").write_text(f"STATE_DIR={tmp_path}/state\nPORT=8123\n")
    (tmp_path / "compose.yaml").write_text(
        "services:\n  a:\n    image: busybox\n    profiles: [x]\n"
        "    env_file: [app.env, '${STATE_DIR:?set}/service/s.env', {path: missing.env, required: false}]\n"
        "    environment: {OVERRIDDEN: from-compose, DERIVED: '${PORT}', LITERAL: 'lit$$eral'}\n"
        "    ports: ['${PORT}:80']\n"
        "    healthcheck: {test: [CMD-SHELL, 'pg_isready -U synthetic_owner']}\n"
        "    volumes: ['${STATE_DIR}:/data/x']\n")
    project = {"name": "synthetic-real", "directory": str(tmp_path), "files": [str(tmp_path / "compose.yaml")],
               "env_files": [str(tmp_path / "interp.env")], "services": {"a": "runtime"}}
    p.check_env_references()
    resolved = json.loads(p.run(p.compose(project) + ["config", "--format", "json"]))
    document, named = p.evidence_document(project, resolved)
    assert named == sorted(str(path) for path in (tmp_path / "app.env", tmp_path / "missing.env", tmp_path / "state/service/s.env"))
    written = tmp_path / "written.json"
    p.write(written, p.escaped_rendered(document))
    text = written.read_text()
    assert canary not in text and dollar not in text and dollar.replace("$", "$$") not in text
    reread = p.run(["docker", "compose", "--profile", "*", "-f", str(written), "config", "--format", "json"])
    assert json.loads(reread) == resolved
    assert all(entry["path"].startswith("/") for entry in document["services"]["a"]["env_file"])
    # The real paths: a `$` in a file name, a variable in the middle of an absolute path, and a value needing its service's context.
    dollars = tmp_path / "state" / "a$b.env"
    # API_OVERRIDDEN is explicit in the service, with the value of a credential of that context-dependent file.
    dollars.write_text(f"DOLLAR_CREDENTIAL={canary}-dollars\nENDPOINT=https://${{REVIEW_HOST:?set}}/v1\n"
                       f"API_OVERRIDDEN={canary}-overridden # a comment\nAPI_QUOTED='{canary}-quoted' # a comment\n")
    middle = tmp_path / "opt" / "m.env"
    middle.parent.mkdir()
    middle.write_text(f"MIDDLE_CREDENTIAL={canary}-middle\n")
    (tmp_path / "paths.env").write_text(f"MIDDLE_DIR=opt\nROOT={tmp_path}\n")
    (tmp_path / "paths.yaml").write_text(
        "services:\n  a:\n    image: busybox\n    environment: {REVIEW_HOST: example.invalid}\n"
        "    env_file: ['${ROOT}/state/a$$b.env', '" + str(tmp_path) + "/${MIDDLE_DIR}/m.env']\n")
    paths = {**project, "files": [str(tmp_path / "paths.yaml")], "env_files": [str(tmp_path / "paths.env")]}
    resolved = json.loads(p.run(p.compose(paths) + ["config", "--format", "json"]))
    document, named = p.evidence_document(paths, resolved)
    assert named == sorted([str(dollars), str(middle)])
    p.write(written, p.escaped_rendered(document))
    assert canary not in written.read_text() and "example.invalid/v1" not in written.read_text()
    reread = p.run(["docker", "compose", "--profile", "*", "-f", str(written), "config", "--format", "json"])
    assert json.loads(reread) == resolved
    (tmp_path / "paths.yaml").write_text(
        "services:\n  a:\n    image: busybox\n    environment: {REVIEW_HOST: example.invalid, API_OVERRIDDEN: '${PUBLIC_VALUE}'}\n"
        "    env_file: ['${ROOT}/state/a$$b.env']\n")
    (tmp_path / "paths.env").write_text(f"PUBLIC_VALUE={canary}-overridden\nROOT={tmp_path}\n")
    with pytest.raises(p.Refused, match="env_value_in_evidence"):
        p.evidence_document(paths, json.loads(p.run(p.compose(paths) + ["config", "--format", "json"])))
    (tmp_path / "paths.yaml").write_text(
        "services:\n  a:\n    image: busybox\n    environment: {REVIEW_HOST: example.invalid, API_QUOTED: '${PUBLIC_VALUE}'}\n"
        "    env_file: ['${ROOT}/state/a$$b.env']\n")
    (tmp_path / "paths.env").write_text(f"PUBLIC_VALUE={canary}-quoted\nROOT={tmp_path}\n")
    with pytest.raises(p.Refused, match="env_value_in_evidence"):
        p.evidence_document(paths, json.loads(p.run(p.compose(paths) + ["config", "--format", "json"])))
    document, _ = p.evidence_document(project, json.loads(p.run(p.compose(project) + ["config", "--format", "json"])))
    document["services"]["a"]["command"] = ["--note", canary]
    applied = p.applied_values(project, [{"path": str(tmp_path / "app.env")}], {}, set())
    with pytest.raises(p.Refused, match="env_value_in_evidence"):
        p.check_evidence(project, document, p.evidence_source(project), applied)


# spec: ANYM-773
def test_production_md_names_the_env_file_writers_codes_and_the_rollback_rule():
    docs = " ".join((ROOT / "docs/operations/production.md").read_text().split())
    section = docs.split("Release evidence and env files", 1)[1].split("### Migration releases", 1)[0]
    for text in ("`compose_ignores_env_resolution`", "`env_value_in_evidence`", "Rollback restores images and Compose settings, not env-file values",
                 "`<project>.json`", "`<project>.before.json`", "`gateway.pre-run.json`", "`http.json`", "`verify.env`", "policy copies",
                 "resolved documents", "ANYM-779", "ANYM-784", "2.40.3"):
        assert text in section, text


def rolled_back_refusal(host, match):
    host.calls.clear()
    with pytest.raises(p.Refused, match=match):
        p.rollback(host.settings, RELEASES[1])
    assert host.mutations() == [] and not (host.state / RELEASES[1] / "rollback.json").exists()


# spec: ANYM-488
def test_rollback_and_prepare_do_not_check_the_host_backup_runner(tmp_path, monkeypatch):
    host = Host(tmp_path, monkeypatch)
    for sha in RELEASES[:2]:
        host.release(sha)
    stale = tmp_path / "stale_runner.py"
    stale.write_bytes(b"stale runner\n")
    monkeypatch.setattr(p, "BACKUP_RUNNER", stale)
    def checked(*args):
        raise AssertionError("the host backup runner was read")
    monkeypatch.setattr(p, "check_backup_runner", checked)
    assert p.rollback(host.settings, RELEASES[1])["status"] == "rolled-back"
    host.main = RELEASES[2]
    p.prepare(host.settings, RELEASES[2])
    with pytest.raises(AssertionError, match="backup runner was read"):
        p.apply(host.settings, RELEASES[2])


@pytest.mark.parametrize("drift", ["recreated-same-image", "other-image", "compose-configuration"])
def test_rollback_is_refused_when_production_changed_since_apply(tmp_path, monkeypatch, drift):
    host = Host(tmp_path, monkeypatch)
    for sha in RELEASES[:2]:
        host.release(sha)
    if drift == "recreated-same-image":
        # Same image, but recreated (for example with other mounts or environment): another container.
        host.container = {**host.container, "Id": "recreated"}
    elif drift == "other-image":
        host.container = {**host.container, "Image": OLD_IMAGE}
    else:
        document = json.loads(host.original.read_text())
        document["services"]["api"]["environment"]["SYNTHETIC"] = "changed"
        host.original.write_text(json.dumps(document))
    rolled_back_refusal(host, "changed since this release was applied")


@pytest.mark.parametrize("case", ["modified", "missing"])
def test_rollback_is_refused_when_the_before_release_snapshot_is_modified_or_missing(tmp_path, monkeypatch, case):
    host = Host(tmp_path, monkeypatch)
    for sha in RELEASES[:2]:
        host.release(sha)
    before = host.state / RELEASES[1] / "synthetic.before.json"
    if case == "missing":
        before.unlink()
    else:
        document = p.read(before)
        document["services"]["api"]["image"] = OLD_IMAGE
        p.write(before, document)
    rolled_back_refusal(host, "before-release snapshot")


def test_rollback_never_pulls_a_previous_image_that_is_gone(tmp_path, monkeypatch):
    host = Host(tmp_path, monkeypatch)
    for sha in RELEASES[:2]:
        host.release(sha)
    del host.images[host.image(RELEASES[0])]
    rolled_back_refusal(host, "no longer present on this host")
    assert not any(call[:2] == ["docker", "pull"] for call in host.calls)
    assert not (host.state / RELEASES[1] / "rollback-attempted.json").exists()


@pytest.mark.parametrize("marker", ["attempted.json", "rollback-attempted.json"])
def test_rollback_is_refused_while_an_apply_or_rollback_is_unfinished(tmp_path, monkeypatch, marker):
    host = Host(tmp_path, monkeypatch)
    for sha in RELEASES[:2]:
        host.release(sha)
    unfinished = host.state / ("4" * 40)
    unfinished.mkdir()
    p.write(unfinished / marker, {"sha": "4" * 40})
    rolled_back_refusal(host, "unfinished")


def test_a_failed_rollback_is_not_retried(tmp_path, monkeypatch):
    host = Host(tmp_path, monkeypatch)
    for sha in RELEASES[:2]:
        host.release(sha)
    original = host.run
    def failing(argv, **kwargs):
        if "up" in argv:
            raise p.Refused("synthetic start failure")
        return original(argv, **kwargs)
    monkeypatch.setattr(p, "run", failing)
    with pytest.raises(p.Refused, match="synthetic start failure"):
        p.rollback(host.settings, RELEASES[1])
    assert p.read(host.state / "current.json")["status"] == "applied"
    monkeypatch.setattr(p, "run", original)
    rolled_back_refusal(host, "already rolled back or attempted")


def test_a_release_without_rollback_data_keeps_the_manual_procedure(tmp_path, monkeypatch):
    host = Host(tmp_path, monkeypatch)
    for sha in RELEASES[:2]:
        host.release(sha)
    # A release applied before rollback data existed (ANYM-301 B5).
    for path in (host.state / RELEASES[1] / "result.json", host.state / "current.json"):
        result = p.read(path)
        del result["rollback"]
        p.write(path, result)
    rolled_back_refusal(host, "no rollback data")


def test_rollback_is_refused_after_the_production_configuration_changed(tmp_path, monkeypatch):
    host = Host(tmp_path, monkeypatch)
    for sha in RELEASES[:2]:
        host.release(sha)
    host.settings["new_setting"] = True
    rolled_back_refusal(host, "configuration changed")


def retention_state(tmp_path, monkeypatch, *, releases, current, prepared=(), containers=(), tags=()):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    for sha, applied_at, image in releases:
        (state / sha).mkdir()
        p.write(state / sha / "result.json", {"sha": sha, "images": {"runtime": image}, "status": "applied",
                                               **({"applied_at": applied_at} if applied_at else {})})
    for sha, image in prepared:
        (state / sha).mkdir()
        p.write(state / sha / "prepared.json", {"sha": sha, "images": {"runtime": image}})
    sha, image, before = current
    p.write(state / "current.json", p.read(state / sha / "result.json") | {
        "rollback": {"version": 1, "projects": [{"name": "synthetic"}]}})
    p.write(state / sha / "synthetic.before.json", {"services": {"api": {"image": before}, "db": {"image": "postgres:pinned"}}})
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["docker", "ps"]:
            return "".join(f"{container}\n" for container, _ in containers)
        if argv[:2] == ["docker", "inspect"]:
            return json.dumps([{"Id": container, "Image": image} for container, image in containers])
        if argv[:3] == ["docker", "image", "ls"]:
            return "".join(f"{name} {image}\n" for name, image in tags)
        raise AssertionError(argv)
    monkeypatch.setattr(p, "run", run)
    return {"version": 1, "state_directory": str(state), "projects": [{"name": "synthetic", "services": {"api": "runtime"}}]}, calls


def image_id(seed):
    return "sha256:" + seed * 64


def test_retention_report_keeps_current_rollback_recent_prepared_and_container_images(tmp_path, monkeypatch):
    sha = [str(n) * 40 for n in range(1, 8)]
    settings, calls = retention_state(tmp_path, monkeypatch, releases=[
        (sha[0], "2026-10-01T00:00:00Z", image_id("1")), (sha[1], "2026-10-02T00:00:00Z", image_id("2")),
        (sha[2], "2026-10-03T00:00:00Z", image_id("3")), (sha[3], "2026-10-04T00:00:00Z", image_id("4")),
        (sha[4], None, image_id("5")), (sha[5], "2026-10-05T00:00:00Z", image_id("6"))],
        current=(sha[5], image_id("6"), image_id("4")), prepared=[(sha[6], image_id("7"))],
        containers=[("stopped0", image_id("1"))],
        tags=[(f"deedkit-production-runtime:{sha[n]}", image_id(str(n + 1))) for n in range(7)]
        + [("deedkit-production-runtime:extra", image_id("2")), ("deedkit-production-http-router:" + sha[0], image_id("8")),
           ("postgres:pinned", image_id("9"))])
    report = p.images_report(settings)
    kept = {item["image"]: item["reasons"] for item in report["keep"]}
    assert kept[image_id("6")] == ["current release " + sha[5]]
    assert kept[image_id("4")] == ["rollback target of the current release"]
    assert kept[image_id("7")] == [f"prepared release {sha[6]} is not applied"]
    assert kept[image_id("1")] == ["used by container stopped0"]
    # Only the eight CI-published targets' production tags are reported. Older applied releases, dated or not, are
    # candidates (ANYM-439: the current release and one previous are kept, no longer the two newest others).
    reason = "not the current release, its rollback target, prepared, named by a host receipt or used by a container"
    assert report["candidates"] == [
        {"image": image_id("2"), "reason": reason,
         "tags": [f"deedkit-production-runtime:{sha[1]}", "deedkit-production-runtime:extra"]},
        {"image": image_id("3"), "reason": reason, "tags": [f"deedkit-production-runtime:{sha[2]}"]},
        {"image": image_id("5"), "reason": reason, "tags": [f"deedkit-production-runtime:{sha[4]}"]}]
    assert set(kept) | {image_id(n) for n in "235"} == {image_id(str(n)) for n in range(1, 8)}
    # It only reads: no removal, prune or tag change.
    assert all(call[:2] in (["docker", "ps"], ["docker", "inspect"]) or call[:3] == ["docker", "image", "ls"] for call in calls)
    assert "--all" in calls[0]


def test_retention_report_after_a_rollback_keeps_the_restored_images(tmp_path, monkeypatch):
    host = Host(tmp_path, monkeypatch)
    for sha in RELEASES:
        host.release(sha)
    p.rollback(host.settings, RELEASES[2])
    original = host.run
    def run(argv, **kwargs):
        if argv[:3] == ["docker", "image", "ls"] and "--format" in argv:
            return "".join(f"deedkit-production-runtime:{sha} {host.image(sha)}\n" for sha in RELEASES)
        return original(argv, **kwargs)
    monkeypatch.setattr(p, "run", run)
    kept = {item["image"]: item["reasons"] for item in p.images_report(host.settings)["keep"]}
    assert "restored by the rollback of " + RELEASES[2] in kept[host.image(RELEASES[1])]
    assert kept[host.image(RELEASES[2])][0] == "rolled-back release " + RELEASES[2]
    # The first release is neither restored nor the rollback target any more (ANYM-439).
    assert set(kept) == {host.image(sha) for sha in RELEASES[1:]}


LIFECYCLE_SERVICES = {"file-lifecycle": "runtime", "cold-archive": "backup", "cold-restore": "backup"}


class WorkersHost(Host):
    """A synthetic host whose only project is the lifecycle entry of deploy/production.example.json, on the real Compose
    units of deploy/compose.file-lifecycle.yaml, with one runtime and one backup image per revision. No Docker."""

    def __init__(self, tmp_path, monkeypatch):
        super().__init__(tmp_path, monkeypatch)
        document = resolved(yaml.load((ROOT / "deploy/compose.file-lifecycle.yaml").read_text(), Loader=ComposeLoader))
        for unit in document["services"].values():
            unit["volumes"] = [volume if isinstance(volume, dict) else dict(zip(("source", "target"), volume.split(":")[:2]))
                               for volume in unit["volumes"]]
        document = {**document, "name": "deedkit-file-lifecycle",
                    "services": {**document["services"], "db": {"image": "postgres:pinned"}}}
        original = tmp_path / "file-lifecycle.json"
        original.write_text(json.dumps(document))
        example = json.loads((ROOT / "deploy/production.example.json").read_text())
        entry = next(item for item in example["projects"] if item["name"] == "deedkit-file-lifecycle")
        self.project = {**entry, "directory": str(tmp_path), "files": [str(original)]}
        self.settings["projects"] = [self.project]
        self.before = {service: image_id(seed) for service, seed in zip(LIFECYCLE_SERVICES, "ef0")}
        self.images.update({image: {"Id": image, "Config": {"Labels": {}}} for image in self.before.values()})
        self.containers = {service: {"Id": f"{service}-old", "Image": image, "Config": {"Labels": {}},
                                     "State": {"Running": True, "Status": "running"}}
                           for service, image in self.before.items()}
        monkeypatch.setattr(p, "published", self.worker_published)

    @staticmethod
    def worker_image(sha, target):
        return "sha256:" + hashlib.sha256(f"ci {target} {sha}".encode()).hexdigest()

    def worker_published(self, target, sha):
        return verified_image(target, self.worker_image(sha, target),
                              "sha256:" + hashlib.sha256(f"{target} {sha}".encode()).hexdigest())

    def inspected(self, identity):
        return next(unit for unit in self.containers.values() if unit["Id"] == identity)

    def run(self, argv, **kwargs):
        if argv[:2] == ["docker", "pull"]:
            target = argv[-1].partition("@")[0].removeprefix(p.REGISTRY)
            image = self.worker_image(self.main, target)
            self.images[image] = ImageStore.image("c", target, revision=self.main, source=self.main) | {"Id": image}
            return ""
        if argv[:2] == ["docker", "tag"]:
            target = argv[-1].partition(":")[0].removeprefix("deedkit-production-")
            self.tags[argv[-1]] = self.worker_image(self.main, target)
            return ""
        if argv[:3] == ["docker", "image", "inspect"] and "@" in argv[-1]:
            return self.worker_image(self.main, argv[-1].partition("@")[0].removeprefix(p.REGISTRY))
        if argv[:2] == ["docker", "compose"] and "config" not in argv:
            self.calls.append(argv)
            if "stop" in argv:
                for service in argv[argv.index("stop") + 1:]:
                    self.containers[service]["State"] = {"Running": False, "Status": "exited"}
            if "up" in argv:
                units = json.loads(Path(argv[argv.index("-f") + 1]).read_text())["services"]
                for service in argv[argv.index("--wait-timeout") + 2:]:
                    self.serial += 1
                    self.containers[service] = {
                        "Id": f"{service}-{self.serial}", "Image": units[service]["image"],
                        "Config": {"Labels": units[service].get("labels", {})},
                        "State": {"Running": True, "Status": "running"}}
            return self.containers[argv[-1]]["Id"] + "\n" if "ps" in argv else ""
        if argv[:2] == ["docker", "ps"]:
            self.calls.append(argv)
            service = next(item.rpartition("=")[2] for item in argv if item.startswith("label=com.docker.compose.service="))
            unit = self.containers.get(service)
            return unit["Id"] + "\n" if unit and ("--all" in argv or unit["State"]["Running"]) else ""
        if argv[:2] == ["docker", "inspect"]:
            self.calls.append(argv)
            unit = self.inspected(argv[-1])
            if "--format" in argv:
                return f"{unit['State']['Status']} {unit['Config']['Labels'].get('org.opencontainers.image.revision', '')}\n"
            return json.dumps([unit])
        return super().run(argv, **kwargs)

    def running(self):
        return {service: unit["Image"] for service, unit in self.containers.items()}

    def on(self, sha):
        return {service: self.worker_image(sha, target) for service, target in LIFECYCLE_SERVICES.items()}


# spec: ANYM-525
def test_the_lifecycle_project_of_the_example_follows_every_release_and_rollback(tmp_path, monkeypatch):
    host = WorkersHost(tmp_path, monkeypatch)
    # The ordinary project shape through the validated configuration: no new key or mechanism.
    assert set(host.project) == {"name", "directory", "files", "services"} and host.project["services"] == LIFECYCLE_SERVICES
    path = tmp_path / "production.json"
    p.write(path, host.settings)
    path.chmod(0o600)
    assert p.config(path)["projects"] == [host.project]
    # `--profile *` loads the workers' profiles; a release selects exact names and leaves the database alone.
    assert p.compose(host.project)[2:4] == ["--profile", "*"]
    for sha in RELEASES[:2]:
        host.calls.clear()
        assert host.release(sha)["images"] == {target: host.worker_image(sha, target) for target in ("backup", "runtime")}
        assert host.running() == host.on(sha)
        for verb in ("stop", "up"):
            [call] = [call for call in host.calls if call[:2] == ["docker", "compose"] and verb in call]
            assert call[-3:] == list(LIFECYCLE_SERVICES)
        written = p.read(host.state / sha / "deedkit-file-lifecycle.json")["services"]
        assert all(written[service]["labels"]["org.opencontainers.image.revision"] == sha for service in LIFECYCLE_SERVICES)
        assert written["db"]["image"] == "postgres:pinned"
    assert p.rollback(host.settings, RELEASES[1])["status"] == "rolled-back"
    assert host.running() == host.on(RELEASES[0])


# spec: ANYM-525
def test_a_first_rollback_returns_the_workers_to_the_images_they_ran_before_the_project_joined(tmp_path, monkeypatch):
    host = WorkersHost(tmp_path, monkeypatch)
    before = dict(host.before)
    host.release(RELEASES[0])
    p.rollback(host.settings, RELEASES[0])
    assert host.running() == before


class Images:
    """A synthetic Docker 29 image store (containerd snapshotter) for image retention: images with every stored
    reference, containers using them, and `docker image rm` that, like Docker without --force, refuses an image a
    container uses and removes by ID only an image a single reference names. No Docker daemon."""

    def __init__(self, monkeypatch, images, containers=()):
        self.references = {image: list(names) for image, names in images}
        self.containers, self.calls, self.hook = dict(containers), [], lambda argv: None
        monkeypatch.setattr(p, "run", self.run)

    @staticmethod
    def refused(detail):
        return p.Refused(f"docker failed (exit 1): Error response from daemon: {detail}; production was not accepted")

    def run(self, argv, **kwargs):
        self.calls.append(argv)
        self.hook(argv)
        if argv[:2] == ["docker", "ps"]:
            return "".join(container + "\n" for container in self.containers)
        if argv[:2] == ["docker", "inspect"]:
            return json.dumps([{"Id": container, "Image": self.containers[container]} for container in argv[2:]])
        if argv[:3] == ["docker", "image", "ls"]:
            return "".join(f"{name.partition('@')[0] + ':<none>' if '@' in name else name} {image}\n"
                           for image, names in self.references.items() for name in names)
        if argv[:3] == ["docker", "image", "inspect"]:
            if argv[-1] not in self.references:
                raise p.Refused("docker failed (exit 1); production was not accepted")
            return json.dumps([{"Id": argv[-1], "RepoTags": self.references[argv[-1]]}])
        if argv[:4] == ["docker", "image", "rm", "--no-prune"] and len(argv) == 5:
            name = argv[4]
            image = name if name in self.references else next(
                image for image, names in self.references.items() if name in names)
            names, short = self.references[image], image[7:19]
            if name == image and len(names) > 1:
                raise self.refused(f"conflict: unable to delete {short} (must be forced) - image is referenced in "
                                   "multiple repositories")
            users = [container for container, used in self.containers.items() if used == image]
            if users and (name == image or names == [name]):
                raise self.refused(f"conflict: unable to delete {short} (cannot be forced) - image is being used by "
                                   f"running container {users[0][:12]}")
            if name == image:
                names.clear()
            else:
                names.remove(name)
            if not names:
                del self.references[image]
            return ""
        raise AssertionError(argv)

    def removals(self):
        return [call for call in self.calls if call[:3] == ["docker", "image", "rm"]]


def released(state, sha, image, before):
    """One applied release as prepare and apply leave it: receipts, Compose snapshots and current.json."""
    (state / sha).mkdir(parents=True)
    p.write(state / sha / "prepared.json", {"sha": sha, "images": {"runtime": image}, "projects": [{"name": "synthetic"}]})
    p.write(state / sha / "synthetic.json", {"services": {"api": {"image": image}}})
    p.write(state / sha / "synthetic.before.json", {"services": {"api": {"image": before}, "db": {"image": "postgres"}}})
    result = {"sha": sha, "images": {"runtime": image}, "status": "applied", "applied_at": "2026-10-20T00:00:00Z"}
    p.write(state / sha / "result.json", result)
    p.write(state / "current.json", result)


def retention_settings(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700, exist_ok=True)
    return {"version": 1, "state_directory": str(state), "projects": [{"name": "synthetic", "services": {"api": "runtime"}}]}


def runtime(seed):
    """The image ID and production tag of the synthetic runtime release `seed * 40`."""
    return image_id(seed), [f"deedkit-production-runtime:{seed * 40}"]


def candidates(settings):
    return [item["image"] for item in p.images_report(settings)["candidates"]]


# spec: ANYM-439
def test_of_four_completed_releases_only_the_current_and_its_rollback_target_are_kept(tmp_path, monkeypatch):
    settings = retention_settings(tmp_path)
    state = Path(settings["state_directory"])
    for seed, before in (("1", OLD_IMAGE), ("2", image_id("1")), ("3", image_id("2")), ("4", image_id("3"))):
        released(state, seed * 40, image_id(seed), before)
    store = Images(monkeypatch, [runtime(seed) for seed in "1234"])
    kept = {item["image"]: item["reasons"] for item in p.images_report(settings)["keep"]}
    # Historical receipts and snapshots of earlier releases keep nothing.
    assert kept == {image_id("4"): ["current release " + "4" * 40], image_id("3"): ["rollback target of the current release"]}
    summary = p.remove_images(settings)
    assert summary == {"removed": [{"image": "1" * 12, "tags": runtime("1")[1]}, {"image": "2" * 12, "tags": runtime("2")[1]}],
                       "failed": [], "kept": 2}
    assert set(store.references) == {image_id("3"), image_id("4")}


# spec: ANYM-439
@pytest.mark.parametrize("where", ["nested", "external"])
def test_another_states_current_rollback_and_prepared_images_are_kept(tmp_path, monkeypatch, where):
    settings = retention_settings(tmp_path)
    released(Path(settings["state_directory"]), "1" * 40, image_id("1"), OLD_IMAGE)
    p.HOST_STATE.mkdir(parents=True)
    p.CONFIG_DIRECTORY.mkdir(parents=True)
    other = p.HOST_STATE / "other-stack" if where == "nested" else tmp_path / "external"
    if where == "external":
        p.write(p.CONFIG_DIRECTORY / "other.json", {"version": 1, "state_directory": str(other)})
    released(other, "a" * 40, image_id("a"), image_id("b"))
    (other / ("c" * 40)).mkdir()
    p.write(other / ("c" * 40) / "prepared.json", {"sha": "c" * 40, "images": {"runtime": image_id("c")}})
    store = Images(monkeypatch, [runtime(seed) for seed in "1abc9"])
    kept = {item["image"]: item["reasons"] for item in p.images_report(settings)["keep"]}
    assert kept[image_id("a")] == [f"current release {'a' * 40} of {other}"]
    assert kept[image_id("b")] == [f"rollback target of the current release of {other}"]
    assert kept[image_id("c")] == [f"prepared release {'c' * 40} of {other} is not applied"]
    assert p.remove_images(settings)["removed"] == [{"image": "9" * 12, "tags": runtime("9")[1]}]
    assert set(store.references) == {image_id(seed) for seed in "1abc"}


# spec: ANYM-439
def test_a_busy_state_keeps_the_revisions_it_may_be_preparing_and_removal_holds_the_free_ones(tmp_path, monkeypatch):
    import fcntl
    settings = retention_settings(tmp_path)
    released(Path(settings["state_directory"]), "1" * 40, image_id("1"), OLD_IMAGE)
    other = p.HOST_STATE / "other-stack"
    released(other, "a" * 40, image_id("a"), OLD_IMAGE)
    # A preparation creates its release directory, then pulls; its prepared.json comes only at the end.
    (other / ("e" * 40)).mkdir()
    store = Images(monkeypatch, [runtime(seed) for seed in "1ae"])
    with (other / "deploy.lock").open("a") as preparation:
        fcntl.flock(preparation, fcntl.LOCK_EX | fcntl.LOCK_NB)
        kept = {item["image"]: item["reasons"] for item in p.images_report(settings)["keep"]}
        assert kept[image_id("e")] == [f"release {'e' * 40} of {other} may be preparing now"]
        # Removal waits for the next pass: it would also take the image lock that preparation needs.
        assert p.retain_images(settings) == {
            "skipped": f"another production operation holds the deploy lock of {other}; no image is removed"}
        assert store.removals() == [] and image_id("e") in store.references
    # Idle, the other state's lock is held for the whole removal, so no preparation can start there meanwhile.
    held = []
    def probe(argv):
        if argv[:3] == ["docker", "image", "rm"]:
            with (other / "deploy.lock").open("a") as preparation:
                try:
                    fcntl.flock(preparation, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    held.append(False)
                except BlockingIOError:
                    held.append(True)
    store.hook = probe
    assert p.remove_images(settings)["removed"] == [{"image": "e" * 12, "tags": runtime("e")[1]}]
    assert held == [True]


# spec: ANYM-439
def test_http_state_keeps_the_serving_and_one_previous_generation_and_unattempted_preparations(tmp_path, monkeypatch):
    settings = retention_settings(tmp_path)
    released(Path(settings["state_directory"]), "1" * 40, image_id("1"), OLD_IMAGE)
    http = tmp_path / "dk-http"
    p.CONFIG_DIRECTORY.mkdir(parents=True)
    p.write(p.CONFIG_DIRECTORY / "http-production.json", {"version": 1, "state_directory": str(http)})
    http.mkdir()
    p.write(http / "http-current.json", {"sha": "a" * 40, "status": "serving", "images": {"runtime": image_id("a")},
                                         "previous": {"sha": "b" * 40, "images": {"runtime": image_id("b")},
                                                      "previous": {"sha": "c" * 40, "images": {"runtime": image_id("c")}}}})
    for seed, attempted in (("d", False), ("e", True)):
        (http / (seed * 40)).mkdir()
        p.write(http / (seed * 40) / "prepared.json", {"images": {"runtime": image_id(seed)}})
        p.write(http / (seed * 40) / "http-prepared.json", {"images": {"runtime": image_id(seed)}})
        if attempted:
            p.write(http / (seed * 40) / "http-attempted.json", {"status": "serving"})
    Images(monkeypatch, [runtime(seed) for seed in "1abcde"])
    kept = {item["image"] for item in p.images_report(settings)["keep"]}
    # The retired generation nested two levels deep and an attempted HTTP release no longer keep their images.
    assert kept == {image_id(seed) for seed in "1abd"} and candidates(settings) == [image_id("c"), image_id("e")]


# spec: ANYM-439
def test_foreign_references_untracked_targets_and_pinned_or_protected_images_are_kept(tmp_path, monkeypatch):
    settings = retention_settings(tmp_path)
    state = Path(settings["state_directory"])
    released(state, "1" * 40, image_id("1"), OLD_IMAGE)
    p.CONFIG_DIRECTORY.mkdir(parents=True)
    p.write(p.CONFIG_DIRECTORY / "file-lifecycle.json", {"job": {"image": image_id("2")}})
    p.write(state / "storage-cleanup.json", {"protected_images": [image_id("3")]})
    (p.HOST_STATE / "operator-notes").mkdir(parents=True)
    p.write(p.HOST_STATE / "operator-notes" / "plan.json", {"keep": [image_id("4")]})
    Images(monkeypatch, [runtime("1"), runtime("2"), runtime("3"), runtime("4"), runtime("9"),
                         (image_id("5"), [*runtime("5")[1], "deedkit-rollback-retained:manual"]),
                         (image_id("6"), ["deedkit-production-backup:" + "6" * 40])])
    kept = {item["image"]: item["reasons"] for item in p.images_report(settings)["keep"]}
    assert kept[image_id("2")] == [f"named in {p.CONFIG_DIRECTORY / 'file-lifecycle.json'}"]
    assert kept[image_id("3")] == [f"protected by {state / 'storage-cleanup.json'}"]
    assert kept[image_id("4")] == [f"named in {p.HOST_STATE / 'operator-notes' / 'plan.json'}"]
    assert kept[image_id("5")] == ["also named deedkit-rollback-retained:manual"]
    # The backup runner pins its image by ID in its own configuration, outside these directories.
    assert kept[image_id("6")] == ["target backup is not deployed by this configuration"]
    assert candidates(settings) == [image_id("9")]


def workers_settings(tmp_path, monkeypatch, *, configured=None):
    """A configuration whose lifecycle project deploys the backup target, with one runner.json slot per profile."""
    settings = retention_settings(tmp_path)
    settings["projects"] = [{"name": "deedkit-file-lifecycle", "services": LIFECYCLE_SERVICES}]
    if configured:
        settings["migration"] = {"rehearsal_limit_ms": 1000,
                                 "unattended": {"network": "synthetic", "dsn_file": str(tmp_path / "owner.dsn"),
                                                "runner_config": str(configured)}}
    runners = tmp_path / "runners"
    runners.mkdir()
    monkeypatch.setattr(p, "RUNNER_CONFIGS", tuple(runners / name / "runner.json" for name in
                                                   ("messaging", "calendar", "recordings", "calls")))
    return settings, runners


def backup(seed):
    return image_id(seed), [f"deedkit-production-backup:{seed * 40}"]


def pin(path, image, **fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"image": image, **fields}))
    return path


# spec: ANYM-525
@pytest.mark.parametrize("profile", ["messaging", "calendar", "recordings", "calls", "configured"])
def test_an_idle_runner_pin_older_than_the_workers_images_stays_with_no_backup_container(tmp_path, monkeypatch, profile):
    configured = tmp_path / "ops" / "runner.json" if profile == "configured" else None
    settings, runners = workers_settings(tmp_path, monkeypatch, configured=configured)
    state = Path(settings["state_directory"])
    released(state, "a" * 40, image_id("a"), image_id("b"))
    path = pin(configured or runners / profile / "runner.json", image_id("c"))
    images = Images(monkeypatch, [backup("a"), backup("b"), backup("c"), backup("d")])
    report = p.images_report(settings)
    kept = {item["image"]: item["reasons"] for item in report["keep"]}
    assert kept[image_id("c")] == [f"pinned by {path}"]
    # The other old backup image is no longer kept merely because of its target: the workers deploy it.
    assert [item["image"] for item in report["candidates"]] == [image_id("d")]
    assert p.remove_images(settings)["removed"] == [{"image": "d" * 12, "tags": ["deedkit-production-backup:" + "d" * 40]}]
    assert set(images.references) == {image_id("a"), image_id("b"), image_id("c")}


# spec: ANYM-525
def test_a_runner_pin_by_tag_keeps_the_image_it_resolves_to(tmp_path, monkeypatch):
    settings, runners = workers_settings(tmp_path, monkeypatch)
    released(Path(settings["state_directory"]), "a" * 40, image_id("a"), image_id("b"))
    tag = "deedkit-production-backup:" + "c" * 40
    path = pin(runners / "messaging" / "runner.json", tag)
    images = Images(monkeypatch, [backup("a"), backup("b"), backup("c")])
    real = images.run
    monkeypatch.setattr(p, "run", lambda argv, **kw: image_id("c") + "\n" if argv[-1] == tag else real(argv, **kw))
    kept = {item["image"]: item["reasons"] for item in p.images_report(settings)["keep"]}
    assert kept[image_id("c")] == [f"pinned by {path}"]


# spec: ANYM-525
def test_every_undo_copy_of_a_moved_runner_pin_keeps_its_image(tmp_path, monkeypatch):
    settings, runners = workers_settings(tmp_path, monkeypatch)
    released(Path(settings["state_directory"]), "a" * 40, image_id("a"), image_id("b"))
    path = pin(runners / "messaging" / "runner.json", image_id("a"))
    older = pin(path.with_name("runner.json.before-" + "1" * 40), image_id("c"))
    newer = pin(path.with_name("runner.json.before-" + "2" * 40), image_id("d"))
    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))
    Images(monkeypatch, [backup("a"), backup("b"), backup("c"), backup("d")])
    report = p.images_report(settings)
    kept = {item["image"]: item["reasons"] for item in report["keep"]}
    # `cp -p runner.json.before-<sha> runner.json` is the documented undo of the last move (production.md).
    assert kept[image_id("d")] == [f"pinned by the undo copy {newer}"]
    # An older time proves nothing: `cp -p` keeps the source's, so the last move's copy can be the older one.
    assert kept[image_id("c")] == [f"pinned by the undo copy {older}"]
    assert report["candidates"] == []


# spec: ANYM-525
def test_an_undo_copy_pinned_by_tag_keeps_its_image_and_one_with_no_image_left_holds_nothing_back(tmp_path, monkeypatch):
    settings, runners = workers_settings(tmp_path, monkeypatch)
    released(Path(settings["state_directory"]), "a" * 40, image_id("a"), image_id("b"))
    path = pin(runners / "messaging" / "runner.json", image_id("a"))
    tag = "deedkit-production-backup:" + "c" * 40
    copied = pin(path.with_name("runner.json.before-" + "1" * 40), tag)
    gone = "deedkit-production-backup:" + "e" * 40
    pin(path.with_name("runner.json.before-" + "2" * 40), gone)
    images = Images(monkeypatch, [backup("a"), backup("b"), backup("c"), backup("d")])
    real = images.run
    def run(argv, **kwargs):
        if argv[-1] == gone:
            raise images.refused(f"No such image: {gone}")
        return image_id("c") + "\n" if argv[-1] == tag else real(argv, **kwargs)
    monkeypatch.setattr(p, "run", run)
    report = p.images_report(settings)
    assert {item["image"]: item["reasons"] for item in report["keep"]}[image_id("c")] == [f"pinned by the undo copy {copied}"]
    assert [item["image"] for item in report["candidates"]] == [image_id("d")]


# spec: ANYM-525
def test_a_named_runner_config_is_read_when_another_migration_setting_is_invalid(tmp_path, monkeypatch):
    configured = tmp_path / "ops" / "runner.json"
    settings, _ = workers_settings(tmp_path, monkeypatch, configured=configured)
    settings["migration"]["rehearsal_limit_ms"] = "not a number"
    with pytest.raises(p.Refused):
        p.unattended_settings(settings)
    released(Path(settings["state_directory"]), "a" * 40, image_id("a"), image_id("b"))
    pin(configured, image_id("c"))
    Images(monkeypatch, [backup("a"), backup("b"), backup("c")])
    report = p.images_report(settings)
    assert {item["image"]: item["reasons"] for item in report["keep"]}[image_id("c")] == [f"pinned by {configured}"]
    assert report["candidates"] == []


# spec: ANYM-525
@pytest.mark.parametrize("profile", ["calendar", "recordings", "calls"])
def test_an_installed_runner_with_only_another_profiles_pin_keeps_removing_images(tmp_path, monkeypatch, profile):
    settings, runners = workers_settings(tmp_path, monkeypatch)
    released(Path(settings["state_directory"]), "a" * 40, image_id("a"), image_id("b"))
    p.BACKUP_RUNNER.parent.mkdir(parents=True)
    p.BACKUP_RUNNER.write_text("# synthetic runner\n")
    pin(runners / profile / "runner.json", image_id("c"))
    Images(monkeypatch, [backup("a"), backup("b"), backup("c"), backup("d")])
    assert p.remove_images(settings)["removed"] == [{"image": "d" * 12, "tags": ["deedkit-production-backup:" + "d" * 40]}]


# spec: ANYM-525
def test_the_rollback_guard_covers_each_worker_and_passes_once_they_run_the_serving_schema(monkeypatch):
    project = {"name": "deedkit-file-lifecycle", "services": LIFECYCLE_SERVICES}
    old_workers = {service: image_id(seed) for service, seed in zip(LIFECYCLE_SERVICES, "ef0")}
    serving_runtime, current = image_id("a"), {"schema.sql": "1" * 64, "migrations/067_synthetic.sql": "2" * 64}
    inventories = {serving_runtime: current, **{image: {"schema.sql": "1" * 64} for image in old_workers.values()}}
    monkeypatch.setattr(p, "http_release_tool", lambda: SimpleNamespace(schema_inventory=inventories.__getitem__))
    serving = {"generation": "a" * 12, "sha": "a" * 40, "images": {"runtime": serving_runtime}, "schema": current}
    # Workers that predate the serving schema: a first rollback is refused, before it stops anything (host plan, step 3).
    for service, image in old_workers.items():
        with pytest.raises(p.Refused, match=f"deedkit-file-lifecycle/{service}'s restored image"):
            p.guard_rollback(serving, [(project, None, {service: image})])
    # Brought to the serving release first: the guard passes for the runtime and backup images alike.
    refreshed = {"file-lifecycle": serving_runtime, "cold-archive": image_id("b"), "cold-restore": image_id("b")}
    inventories[image_id("b")] = current
    assert p.guard_rollback(serving, [(project, None, refreshed)]) is None


# spec: ANYM-525
@pytest.mark.parametrize("case", ["broken-json", "not-an-object", "no-image", "image-not-a-string", "unresolved-tag",
                                  "live-tag-gone", "broken-undo-copy", "undo-copy-with-no-image", "undo-tag-lookup-failed",
                                  "configured-missing", "configured-not-a-path", "installed-runner-without-a-pin"])
def test_removal_refuses_when_a_runner_pin_cannot_be_read(tmp_path, monkeypatch, case):
    configured = tmp_path / "ops" / "runner.json" if case.startswith("configured") else None
    settings, runners = workers_settings(tmp_path, monkeypatch, configured=configured)
    released(Path(settings["state_directory"]), "a" * 40, image_id("a"), image_id("b"))
    path = runners / "calendar" / "runner.json"
    if case == "configured-not-a-path":
        settings["migration"]["unattended"]["runner_config"] = "relative/runner.json"
    elif case == "undo-copy-with-no-image":
        pin(path, image_id("a"))
        path.with_name("runner.json.before-" + "1" * 40).write_text("{}")
    elif case == "undo-tag-lookup-failed":
        # The image is there and every other Docker read answers: this one failed, so nothing says it is gone.
        pin(path, image_id("a"))
        pin(path.with_name("runner.json.before-" + "1" * 40), "deedkit-production-backup:" + "c" * 40)
    elif case == "live-tag-gone":
        pin(path, "deedkit-production-backup:" + "e" * 40)
    elif case == "installed-runner-without-a-pin":
        p.BACKUP_RUNNER.parent.mkdir(parents=True)
        p.BACKUP_RUNNER.write_text("# synthetic runner\n")
    elif case == "broken-undo-copy":
        pin(path, image_id("a"))
        path.with_name("runner.json.before-" + "1" * 40).write_text("{")
    elif case == "unresolved-tag":
        pin(path, "deedkit-production-backup:" + "e" * 40)
    elif case == "no-image":
        pin(path, None)
        path.write_text("{}")
    elif case == "image-not-a-string":
        pin(path, 7)
    elif not case.startswith("configured"):
        path.parent.mkdir(parents=True)
        path.write_text({"broken-json": "{", "not-an-object": "[]"}[case])
    images = Images(monkeypatch, [backup("a"), backup("b"), backup("c")])
    if case == "live-tag-gone":
        real = images.run
        def run(argv, **kwargs):
            if argv[-1] == "deedkit-production-backup:" + "e" * 40:
                raise images.refused("No such image")
            return real(argv, **kwargs)
        monkeypatch.setattr(p, "run", run)
    with pytest.raises(p.Refused, match="no image is removed"):
        p.images_report(settings)
    with pytest.raises(p.Refused, match="no image is removed"):
        p.remove_images(settings)
    assert images.removals() == []
    # A release's own removal only reports it: it never changes the release's outcome.
    assert "no image is removed" in RETAIN_IMAGES(settings)["skipped"]
    assert images.removals() == []


# spec: ANYM-525
def test_a_host_without_a_runner_has_no_pin_to_read(tmp_path, monkeypatch):
    settings, _ = workers_settings(tmp_path, monkeypatch)
    released(Path(settings["state_directory"]), "a" * 40, image_id("a"), image_id("b"))
    Images(monkeypatch, [backup("a"), backup("b"), backup("c")])
    assert [item["image"] for item in p.images_report(settings)["candidates"]] == [image_id("c")]


# spec: ANYM-439
def test_removal_takes_the_other_references_first_keeps_a_release_tag_to_the_end_and_never_forces(tmp_path, monkeypatch):
    settings = retention_settings(tmp_path)
    released(Path(settings["state_directory"]), "1" * 40, image_id("1"), OLD_IMAGE)
    image, (tag,) = runtime("9")
    registry = p.REGISTRY + "runtime@sha256:" + "f" * 64
    store = Images(monkeypatch, [runtime("1"), (image, [tag, registry, "deedkit-production-runtime:extra"])])
    summary = p.remove_images(settings)
    assert summary["removed"] == [{"image": "9" * 12, "tags": [tag, "deedkit-production-runtime:extra"]}]
    # The sorted first release tag stays until the image itself goes by ID, after its other references.
    assert store.removals() == [["docker", "image", "rm", "--no-prune", registry],
                                ["docker", "image", "rm", "--no-prune", "deedkit-production-runtime:extra"],
                                ["docker", "image", "rm", "--no-prune", image]]
    assert image not in store.references
    assert not any({"-f", "--force", "prune"} & set(call) or "system" in call for call in store.calls)


# spec: ANYM-439
def test_a_refused_image_stays_listed_with_dockers_reason_and_removal_goes_on(tmp_path, monkeypatch):
    settings = retention_settings(tmp_path)
    released(Path(settings["state_directory"]), "1" * 40, image_id("1"), OLD_IMAGE)
    store = Images(monkeypatch, [runtime("1"), runtime("7"), runtime("8")])
    def start(argv):
        # A container starts from the first candidate after the report chose it.
        if argv[-1] == image_id("7"):
            store.containers["late-container"] = image_id("7")
    store.hook = start
    summary = p.remove_images(settings)
    assert summary["removed"] == [{"image": "8" * 12, "tags": runtime("8")[1]}]
    assert summary["failed"] == [{"image": "7" * 12, "tags": runtime("7")[1], "reason":
                                  "docker failed (exit 1): Error response from daemon: conflict: unable to delete "
                                  "777777777777 (cannot be forced) - image is being used by running container late-contain"}]
    assert store.references[image_id("7")] == runtime("7")[1]


# spec: ANYM-439
def test_an_interrupted_removal_reports_its_progress_and_the_next_pass_finds_the_image_again(tmp_path, monkeypatch):
    settings = retention_settings(tmp_path)
    released(Path(settings["state_directory"]), "1" * 40, image_id("1"), OLD_IMAGE)
    image, (tag,) = runtime("9")
    registry = p.REGISTRY + "runtime@sha256:" + "f" * 64
    store = Images(monkeypatch, [runtime("1"), runtime("8"), (image, [tag, registry])])
    def timeout(argv):
        if argv == ["docker", "image", "rm", "--no-prune", image]:
            raise subprocess.TimeoutExpired(argv, 1800)
    store.hook = timeout
    summary = p.remove_images(settings)
    assert summary == {"removed": [{"image": "8" * 12, "tags": runtime("8")[1]}], "failed": [], "kept": 1,
                       "interrupted": "TimeoutExpired at " + "9" * 12, "remaining": 1}
    # Its registry reference went, its release tag stayed: the next pass still sees it.
    assert store.references[image] == [tag] and candidates(settings) == [image]
    store.hook = lambda argv: None
    assert p.remove_images(settings)["removed"] == [{"image": "9" * 12, "tags": [tag]}] and image not in store.references


# spec: ANYM-439
@pytest.mark.parametrize("failure", ["config", "receipt", "image lock", "docker"])
def test_an_unreadable_protecting_source_or_a_failure_before_removal_removes_nothing(tmp_path, monkeypatch, failure):
    import fcntl
    settings = retention_settings(tmp_path)
    state = Path(settings["state_directory"])
    released(state, "1" * 40, image_id("1"), OLD_IMAGE)
    store = Images(monkeypatch, [runtime("1"), runtime("9")])
    monkeypatch.setattr(p, "IMAGE_LOCK", tmp_path / "image-build.lock")
    with contextlib.ExitStack() as stack:
        if failure == "config":
            p.CONFIG_DIRECTORY.mkdir(parents=True)
            (p.CONFIG_DIRECTORY / "broken.json").write_text("{")
            expected = f"cannot read {p.CONFIG_DIRECTORY / 'broken.json'}; no image is removed"
        elif failure == "receipt":
            (state / ("1" * 40) / "synthetic.before.json").write_text("[")
            expected = f"cannot read {state / ('1' * 40) / 'synthetic.before.json'}; no image is removed"
        elif failure == "image lock":
            fcntl.flock(stack.enter_context(p.IMAGE_LOCK.open("a")), fcntl.LOCK_EX | fcntl.LOCK_NB)
            expected = "another image build holds the production image lock"
        else:
            store.hook = lambda argv: (_ for _ in ()).throw(RuntimeError()) if argv[:2] == ["docker", "ps"] else None
            expected = "RuntimeError"
        assert p.retain_images(settings) == {"skipped": expected}
    assert store.removals() == [] and image_id("9") in store.references


# spec: ANYM-439
@pytest.mark.parametrize("failure", ["release directory", "config directory", "missing snapshot", "no snapshot"])
def test_a_protecting_directory_it_cannot_list_or_a_missing_rollback_snapshot_removes_nothing(tmp_path, monkeypatch,
                                                                                             failure):
    settings = retention_settings(tmp_path)
    state = Path(settings["state_directory"])
    released(state, "1" * 40, image_id("1"), image_id("2"))
    current = p.read(state / "current.json") | {"rollback": {"version": 1, "projects": [{"name": "synthetic"}]}}
    p.write(state / "current.json", current)
    p.CONFIG_DIRECTORY.mkdir(parents=True)
    store = Images(monkeypatch, [runtime("1"), runtime("2"), runtime("9")])
    # Path.glob would take an unreadable directory for an empty one and leave the rollback image unprotected.
    unreadable = {"release directory": state / ("1" * 40), "config directory": p.CONFIG_DIRECTORY}.get(failure)
    iterdir = Path.iterdir
    def listing(path):
        if path == unreadable:
            raise PermissionError(13, "Permission denied", str(path))
        return iterdir(path)
    monkeypatch.setattr(Path, "iterdir", listing)
    if failure == "missing snapshot":
        (state / ("1" * 40) / "synthetic.before.json").unlink()
    if failure == "no snapshot":  # a receipt without a rollback record and without any snapshot
        p.write(state / "current.json", {key: value for key, value in current.items() if key != "rollback"})
        (state / ("1" * 40) / "synthetic.before.json").unlink()
    summary = p.retain_images(settings)
    assert set(summary) == {"skipped"} and "no image is removed" in summary["skipped"]
    assert store.removals() == [] and image_id("2") in store.references and image_id("9") in store.references


# spec: ANYM-439
def test_removal_takes_the_other_deploy_locks_before_the_image_lock_so_a_running_preparation_keeps_it(tmp_path,
                                                                                                    monkeypatch):
    import fcntl
    settings = retention_settings(tmp_path)
    released(Path(settings["state_directory"]), "1" * 40, image_id("1"), OLD_IMAGE)
    other = p.HOST_STATE / "other-stack"
    released(other, "a" * 40, image_id("a"), OLD_IMAGE)
    store = Images(monkeypatch, [runtime("1"), runtime("a"), runtime("9")])
    monkeypatch.setattr(p, "IMAGE_LOCK", tmp_path / "image-build.lock")
    image_lock_free = []
    probe = p.free_lock
    def observed(directory, stack):
        # What a preparation holding `other` would meet at its next image lookup, at the moment removal probes it.
        with p.IMAGE_LOCK.open("a") as preparation:
            try:
                fcntl.flock(preparation, fcntl.LOCK_EX | fcntl.LOCK_NB)
                image_lock_free.append(True)
            except BlockingIOError:
                image_lock_free.append(False)
        return probe(directory, stack)
    monkeypatch.setattr(p, "free_lock", observed)
    with (other / "deploy.lock").open("a") as preparation:
        fcntl.flock(preparation, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert p.retain_images(settings) == {
            "skipped": f"another production operation holds the deploy lock of {other}; no image is removed"}
    assert image_lock_free and all(image_lock_free) and store.calls == []


# spec: ANYM-439
@pytest.mark.parametrize("late", ["configured, not created yet", "created during the removal"])
def test_a_state_its_first_lock_pass_cannot_hold_stops_removal_without_taking_a_lock_under_the_image_lock(
        tmp_path, monkeypatch, late):
    import fcntl
    settings = retention_settings(tmp_path)
    released(Path(settings["state_directory"]), "1" * 40, image_id("1"), OLD_IMAGE)
    store = Images(monkeypatch, [runtime("1"), runtime("9")])
    monkeypatch.setattr(p, "IMAGE_LOCK", tmp_path / "image-build.lock")
    other = tmp_path / "first-use"
    p.CONFIG_DIRECTORY.mkdir(parents=True)
    p.HOST_STATE.mkdir(parents=True)
    probes = []
    probe = p.free_lock
    def observed(directory, stack):
        with p.IMAGE_LOCK.open("a") as preparation:
            try:
                fcntl.flock(preparation, fcntl.LOCK_EX | fcntl.LOCK_NB)
                probes.append(True)
            except BlockingIOError:
                probes.append(False)
        return probe(directory, stack)
    monkeypatch.setattr(p, "free_lock", observed)
    if late == "configured, not created yet":
        p.write(p.CONFIG_DIRECTORY / "other.json", {"version": 1, "state_directory": str(other)})
        expected = f"the configured state directory {other} does not exist; no image is removed"
    else:
        image_lock = p.image_lock
        @contextlib.contextmanager
        def admitted():
            with image_lock():
                # A first preparation elsewhere creates its state between the two discovery passes.
                released(p.HOST_STATE / "new-stack", "a" * 40, image_id("a"), OLD_IMAGE)
                yield
        monkeypatch.setattr(p, "image_lock", admitted)
        expected = f"the production state {p.HOST_STATE / 'new-stack'} appeared during the removal; no image is removed"
    assert p.retain_images(settings) == {"skipped": expected}
    # Every deploy lock it probed, it probed with the image lock free; nothing was removed.
    assert all(probes) and store.removals() == [] and image_id("9") in store.references


@pytest.mark.parametrize("case", ["modified", "missing"])
def test_a_before_snapshot_changed_between_prepare_and_apply_is_refused_before_any_stop(tmp_path, monkeypatch, case):
    host = Host(tmp_path, monkeypatch)
    host.release(RELEASES[0])
    host.main = RELEASES[1]
    p.prepare(host.settings, RELEASES[1])
    before = host.state / RELEASES[1] / "synthetic.before.json"
    if case == "missing":
        before.unlink()
    else:
        # Point the rollback at an older image that is still on the host, with other configuration.
        document = p.read(before)
        document["services"]["api"].update(image=OLD_IMAGE, environment={"SYNTHETIC": "changed"})
        p.write(before, document)
    host.calls.clear()
    with pytest.raises(p.Refused, match="before-release snapshot is missing or changed since preparation"):
        p.apply(host.settings, RELEASES[1])
    assert host.mutations() == [] and not (host.state / RELEASES[1] / "attempted.json").exists()


@pytest.mark.parametrize("change", ["other-container", "other-image", "compose-configuration"])
def test_apply_never_certifies_a_runtime_that_changed_while_it_applied(tmp_path, monkeypatch, change):
    host = Host(tmp_path, monkeypatch)
    host.main = RELEASES[0]
    p.prepare(host.settings, RELEASES[0])
    original = host.run
    def run(argv, **kwargs):
        result = original(argv, **kwargs)
        if (argv[:2] == ["docker", "inspect"] and change != "compose-configuration"
                and any("up" in call for call in host.calls)):
            # Right after the replacement check, something else restarts or replaces the service.
            host.container = {**host.container, **({"Id": "unexpected"} if change == "other-container" else {"Image": OLD_IMAGE})}
        if argv[:2] == ["docker", "compose"] and "up" in argv and change == "compose-configuration":
            document = json.loads(host.original.read_text())
            document["services"]["api"]["environment"]["SYNTHETIC"] = "changed"
            host.original.write_text(json.dumps(document))
        return result
    monkeypatch.setattr(p, "run", run)
    with pytest.raises(p.Refused, match="changed while this release was applied"):
        p.apply(host.settings, RELEASES[0])
    # The attempt stays unresolved: no result, no current release, and rollback cannot adopt the drift.
    assert (host.state / RELEASES[0] / "attempted.json").exists()
    assert not (host.state / RELEASES[0] / "result.json").exists() and not (host.state / "current.json").exists()


def test_rollback_is_refused_when_the_container_was_reconfigured_in_place(tmp_path, monkeypatch):
    host = Host(tmp_path, monkeypatch)
    for sha in RELEASES[:2]:
        host.release(sha)
    # `docker update` changes resources and restart policy without a new container or image.
    host.container = {**host.container, "HostConfig": {"Memory": 268435456, "RestartPolicy": {"Name": "always"}}}
    rolled_back_refusal(host, "changed since this release was applied")
    assert not (host.state / RELEASES[1] / "rollback-attempted.json").exists()


def test_apply_is_refused_when_the_container_was_reconfigured_in_place_after_preparation(tmp_path, monkeypatch):
    host = Host(tmp_path, monkeypatch)
    host.main = RELEASES[0]
    p.prepare(host.settings, RELEASES[0])
    host.container = {**host.container, "HostConfig": {"RestartPolicy": {"Name": "no"}}}
    host.calls.clear()
    with pytest.raises(p.Refused, match="changed since preparation"):
        p.apply(host.settings, RELEASES[0])
    assert host.mutations() == []


def test_an_older_release_is_refused_while_a_newer_one_is_current(tmp_path, monkeypatch):
    host = Host(tmp_path, monkeypatch)
    for sha in RELEASES[:2]:
        host.release(sha)
    host.calls.clear()
    with pytest.raises(p.Refused, match="only the current applied release"):
        p.rollback(host.settings, RELEASES[0])
    assert host.mutations() == [] and p.read(host.state / "current.json")["sha"] == RELEASES[1]


def test_the_command_line_prints_prepare_references_and_reaches_rollback_and_images(tmp_path, monkeypatch, capsys):
    host = Host(tmp_path, monkeypatch)
    monkeypatch.setattr(p, "config", lambda path: host.settings)
    sha = RELEASES[0]
    host.main = sha
    p.main(["prepare", "--sha", sha])
    printed = json.loads(capsys.readouterr().out)
    assert set(printed) == {"sha", "ci", "images", "references"}
    assert printed["references"] == {"runtime": host.published("runtime", sha)["reference"]}
    p.main(["apply", "--sha", sha])
    capsys.readouterr()
    p.main(["images"])
    assert set(json.loads(capsys.readouterr().out)) == {"keep", "candidates"}
    with pytest.raises(SystemExit):
        p.main(["rollback", "--sha", sha[:12]])
    assert "40-character" in capsys.readouterr().err and host.container["Image"] == host.image(sha)
    p.main(["rollback", "--sha", sha])
    assert json.loads(capsys.readouterr().out)["previous_images"] == {"synthetic": {"api": OLD_IMAGE}}
    assert host.container["Image"] == OLD_IMAGE


def test_a_release_prepared_without_a_bound_rollback_snapshot_must_be_prepared_again(tmp_path, monkeypatch):
    host = Host(tmp_path, monkeypatch)
    host.main = RELEASES[0]
    p.prepare(host.settings, RELEASES[0])
    # A prepared.json written before the before-snapshot digest was bound at preparation.
    receipt = p.read(host.state / RELEASES[0] / "prepared.json")
    del receipt["projects"][0]["before_file_digest"]
    p.write(host.state / RELEASES[0] / "prepared.json", receipt)
    host.calls.clear()
    with pytest.raises(p.Refused, match="prepare it again"):
        p.apply(host.settings, RELEASES[0])
    assert host.mutations() == [] and not (host.state / RELEASES[0] / "attempted.json").exists()


def test_apply_never_certifies_a_container_reconfigured_in_place_while_it_applied(tmp_path, monkeypatch):
    host = Host(tmp_path, monkeypatch)
    host.main = RELEASES[0]
    p.prepare(host.settings, RELEASES[0])
    original = host.run
    def run(argv, **kwargs):
        result = original(argv, **kwargs)
        if argv[:2] == ["docker", "inspect"] and any("up" in call for call in host.calls):
            # After the replacement check, `docker update` changes it in place: same container and image.
            host.container = {**host.container, "HostConfig": {"Memory": 268435456, "RestartPolicy": {"Name": "always"}}}
        return result
    monkeypatch.setattr(p, "run", run)
    with pytest.raises(p.Refused, match="changed while this release was applied"):
        p.apply(host.settings, RELEASES[0])
    assert (host.state / RELEASES[0] / "attempted.json").exists()
    assert not (host.state / RELEASES[0] / "result.json").exists() and not (host.state / "current.json").exists()


@pytest.mark.parametrize("target", p.PUBLISHED)
def test_the_ci_check_build_of_a_published_target_receives_the_candidate_source_revision(tmp_path, monkeypatch,
                                                                                         no_host_lock, target):
    # CI's `check-image` builds what it publishes; the host's pull path relies on that build's revision.
    store = ImageStore()
    monkeypatch.setattr(p, "run", store.run)
    monkeypatch.setattr(p, "published", lambda *args: pytest.fail("a check build never consults the registry"))
    p.build_image(tmp_path, SHA, target, check=True)
    [build] = store.built()
    assert build[build.index("--build-arg") + 1] == "DEEDKIT_SOURCE_SHA=" + SHA
    assert "org.opencontainers.image.revision=" + SHA in build and store.pulls() == []


def test_container_settings_ignore_the_order_docker_returns_mounts_in():
    # docker inspect lists the same mounts in a different order from call to call (release 7, 2026-10-18).
    mounts = [{"Type": "bind", "Source": f"/opt/synthetic/{name}", "Destination": f"/run/secrets/{name}", "RW": False}
              for name in ("a", "b", "c", "d")]
    inspected = {"Config": {"Env": ["A=1"]}, "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}}, "Mounts": mounts}
    digest = p.container_settings(inspected)
    for order in ([3, 1, 0, 2], [1, 0, 3, 2], [2, 3, 1, 0]):
        assert p.container_settings({**inspected, "Mounts": [mounts[i] for i in order]}) == digest
    # A real change is still drift.
    assert p.container_settings({**inspected, "Mounts": mounts[:3]}) != digest
    assert p.container_settings({**inspected, "Mounts": [*mounts[:3], {**mounts[3], "RW": True}]}) != digest
    assert p.container_settings({**inspected, "Config": {"Env": ["A=2"]}}) != digest
    # Legacy links and inherited ulimits come from map iteration too.
    host = {"RestartPolicy": {"Name": "no"}, "Links": ["/x:/y/x", "/z:/y/z"],
            "Ulimits": [{"Name": "nofile", "Soft": 1, "Hard": 2}, {"Name": "nproc", "Soft": 3, "Hard": 4}]}
    reordered = {**host, "Links": host["Links"][::-1], "Ulimits": host["Ulimits"][::-1]}
    assert p.container_settings({**inspected, "HostConfig": host}) == p.container_settings({**inspected, "HostConfig": reordered})


def test_an_apply_or_rollback_record_from_the_order_dependent_digest_is_refused_with_its_remedy(tmp_path, monkeypatch):
    # A record written by the earlier tool (unversioned digest) is never compared with today's digest.
    host = Host(tmp_path, monkeypatch)
    host.main = RELEASES[0]
    p.prepare(host.settings, RELEASES[0])
    receipt = p.read(host.state / RELEASES[0] / "prepared.json")
    for identity in receipt["projects"][0]["running"].values():
        identity["settings"] = identity["settings"].removeprefix(p.SETTINGS_VERSION)
    p.write(host.state / RELEASES[0] / "prepared.json", receipt)
    host.calls.clear()
    with pytest.raises(p.Refused, match="older production.py .* prepare again"):
        p.apply(host.settings, RELEASES[0])
    assert host.mutations() == [] and not (host.state / RELEASES[0] / "attempted.json").exists()
    (tmp_path / "rollback").mkdir()
    host = Host(tmp_path / "rollback", monkeypatch)
    for sha in RELEASES[:2]:
        host.release(sha)
    result = p.read(host.state / RELEASES[1] / "result.json")
    for identity in result["rollback"]["projects"][0]["running"].values():
        identity["settings"] = identity["settings"].removeprefix(p.SETTINGS_VERSION)
    p.write(host.state / RELEASES[1] / "result.json", result)
    host.calls.clear()
    with pytest.raises(p.Refused, match="older production.py .* manual procedure"):
        p.rollback(host.settings, RELEASES[1])
    assert host.mutations() == []


# Continuous deployment stage 1: verify on any commit main contains, rollback after a crash or a failed health
# wait, and `deploy` (production.md "Continuous deployment"). Synthetic GitHub, git and Docker only.


def test_a_commit_main_has_moved_past_is_still_promotable_and_one_outside_main_is_not():
    get, _, _ = github()
    def answering(answer):
        def moved(path):
            if path == "git/ref/heads/main":
                return {"object": {"sha": "f" * 40}}  # main's head is a later commit
            return answer if path.startswith("compare/main...") else get(path)
        return moved
    for behind in (1, 25):
        assert p.verify(SHA, answering(comparison(SHA, behind=behind)))["run_id"] == 12
    for status in ({"status": "ahead", "ahead_by": 1}, {"status": "diverged", "ahead_by": 2},
                   {"status": "behind", "ahead_by": 0, "merge_base_commit": {"sha": "f" * 40}}):
        with pytest.raises(p.Refused, match="not a commit on main"):
            p.verify(SHA, answering({**comparison(SHA, behind=3), **status}))


def test_within_one_deploy_a_later_verification_rereads_the_runs_but_not_their_jobs():
    get, push, group = queue_github()
    paths = []
    def counted(path):
        paths.append(path)
        return get(path)
    with p.verification_reuse():
        first = p.verify(SHA, counted)
        full = len(paths)
        paths.clear()
        assert p.verify(SHA, counted) == first
        # compare, the push run listing, the queue run listing and the push run listing again; no job listing.
        assert [re.sub(r"\?.*?(event=\w+)?(&.*)?$", lambda match: "?" + (match.group(1) or ""), path)
                for path in paths] == ["compare/main..." + SHA, "actions/workflows/ci.yml/runs?event=push",
                                       "actions/workflows/ci.yml/runs?event=merge_group",
                                       "actions/workflows/ci.yml/runs?event=push"]
    paths.clear()
    p.verify(SHA, counted)
    assert len(paths) == full  # outside a deploy run every verification is complete


@pytest.mark.parametrize("changed", ["push", "merge_group"])
def test_a_rerun_after_the_first_verification_of_a_deploy_is_verified_again_in_full(changed):
    get, push, group = queue_github()
    paths = []
    def counted(path):
        paths.append(path)
        # The rerun's jobs are the same synthetic jobs as its first attempt's.
        return get(path.replace("/attempts/3/", "/attempts/1/" if "runs/40/" in path else "/attempts/2/"))
    with p.verification_reuse():
        assert p.verify(SHA, counted)["run_attempt"] == 1
        (push if changed == "push" else group)["run_attempt"] = 3  # a successful rerun of the same run
        paths.clear()
        again = p.verify(SHA, counted)
        rerun = "runs/40/attempts/3/jobs?" if changed == "push" else "runs/30/attempts/3/jobs?"
        assert any(rerun in path for path in paths)  # the new attempt's jobs are read: a full verification
        assert (again["run_attempt"], again["verify_run_attempt"]) == ((3, 2) if changed == "push" else (1, 3))
        (push if changed == "push" else group)["conclusion"] = "failure"
        (push if changed == "push" else group)["run_attempt"] = 4
        with pytest.raises(p.Refused, match="not a completed success"):
            p.verify(SHA, counted)


@pytest.mark.parametrize("change", [{"run_attempt": 2, "status": "in_progress", "conclusion": None}, {"id": 41}])
def test_a_push_run_change_during_the_queue_read_of_a_reused_verification_is_refused(change):
    get, push, _ = queue_github()
    with p.verification_reuse():
        p.verify(SHA, get)
        def racing(path):
            answer = get(path)
            if "event=merge_group" in path:
                push.update(change)  # a rerun or a newer run starts while the queue run is read
            return answer
        with pytest.raises(p.Refused, match="push run changed while it was being verified"):
            p.verify(SHA, racing)


def test_the_quota_a_deploy_waits_for_covers_every_verification_of_plans_a_and_b(tmp_path, monkeypatch):
    import ast
    import inspect
    import textwrap
    from scripts import seamless_release as s
    def calls(function, name):
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
        return sum(isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) == name
                   for node in ast.walk(tree))
    # seamless prepare runs production.py prepare first.
    plan_a = calls(p.prepare, "promotable") + calls(p.apply, "promotable")
    plan_b = calls(p.prepare, "promotable") + calls(s.prepare, "promotable") + calls(s.promote, "promotable")
    assert (plan_a, plan_b) == (4, 6)
    # No step of a release reads GitHub's REST API or its quota any more (ANYM-406), so there is no quota to wait for.
    steps = (p.prepare, p.apply, p.prepare_router, p.promotable, p.deploy, p.deploy_locked, p.deploy_steps,
             s.bootstrap, s.prepare, s.promote)
    assert not hasattr(p, "QUOTA") and not any(calls(step, name) for step in steps
                                               for name in ("verify", "api", "latest_run", "urlopen", "quota"))
    history, _ = main_repository(tmp_path, monkeypatch)
    accepting_verifier(tmp_path, monkeypatch)
    requests = offline(monkeypatch)
    for _ in range(plan_a + plan_b):
        p.promotable(history[-1])
    assert requests == []


def test_an_exhausted_github_quota_is_refused_with_its_reset_time(monkeypatch):
    def limited(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 403, "rate limit exceeded",
                                     {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1792350189"}, None)
    monkeypatch.setattr(p.urllib.request, "urlopen", limited)
    monkeypatch.setenv("GH_TOKEN", "synthetic-secret")
    with pytest.raises(p.RateLimited, match=r"GitHub rate limit exhausted, resets at \d\d:\d\d:\d\dZ") as refused:
        p.api("compare/main..." + SHA)
    assert "synthetic-secret" not in str(refused.value)


@pytest.mark.parametrize("state", [{"Running": False, "Status": "exited", "ExitCode": 1},
                                   {"Running": True, "Restarting": True, "Status": "restarting"}],
                         ids=["exited", "restarting"])
def test_rollback_restores_the_release_before_a_replacement_that_exited_or_keeps_restarting(tmp_path, monkeypatch,
                                                                                             state):
    host = Host(tmp_path, monkeypatch)
    for sha in RELEASES[:2]:
        host.release(sha)
    host.container["State"] = state
    p.rollback(host.settings, RELEASES[1])
    assert host.container["Image"] == host.image(RELEASES[0]) and host.container["State"]["Running"]


def test_rollback_refuses_a_selected_container_that_was_only_created_or_is_paused(tmp_path, monkeypatch):
    host = Host(tmp_path, monkeypatch)
    for sha in RELEASES[:2]:
        host.release(sha)
    for state in ({"Running": False, "Status": "created"}, {"Running": True, "Paused": True, "Status": "paused"}):
        host.container["State"] = state
        rolled_back_refusal(host, "neither running, restarting nor exited")


def unhealthy_apply(host, monkeypatch, *, image=None):
    """Apply RELEASES[1] over RELEASES[0] while its replacement starts and then fails Compose's health wait."""
    host.release(RELEASES[0])
    host.main = RELEASES[1]
    p.prepare(host.settings, RELEASES[1])
    original = host.run
    def run(argv, **kwargs):
        result = original(argv, **kwargs)
        if argv[:2] == ["docker", "compose"] and "up" in argv and "--wait" in argv:
            host.container["State"] = {"Running": True, "Restarting": True, "Status": "restarting"}
            if image:
                host.container["Image"] = image
            raise p.Refused("docker failed (exit 1); production was not accepted")
        return result
    monkeypatch.setattr(p, "run", run)
    with pytest.raises(p.Refused) as refused:
        p.apply(host.settings, RELEASES[1])
    monkeypatch.setattr(p, "run", original)
    return str(refused.value)


def test_a_failed_health_wait_is_recorded_unhealthy_and_rolls_back_to_the_release_before_it(tmp_path, monkeypatch):
    host = Host(tmp_path, monkeypatch)
    message = unhealthy_apply(host, monkeypatch)
    assert "failed its health wait" in message and f"rollback --sha {RELEASES[1]}" in message
    result = p.read(host.state / RELEASES[1] / "result.json")
    assert result["status"] == "unhealthy" and "applied_at" not in result
    assert [item["name"] for item in result["rollback"]["projects"]] == ["synthetic"]
    # It never became current: current.json names the release that ran before it.
    current = p.read(host.state / "current.json")
    assert current["sha"] == RELEASES[0] and current["status"] == "applied"
    with pytest.raises(p.Refused, match="already attempted"):
        p.apply(host.settings, RELEASES[1])
    p.rollback(host.settings, RELEASES[1])
    assert host.container["Image"] == host.image(RELEASES[0]) and host.container["State"] == {"Running": True}
    assert p.read(host.state / "current.json") == current
    host.calls.clear()
    with pytest.raises(p.Refused, match="already rolled back or attempted"):
        p.rollback(host.settings, RELEASES[1])
    assert host.mutations() == []


def test_a_failed_health_wait_without_the_prepared_image_running_stays_unresolved(tmp_path, monkeypatch):
    host = Host(tmp_path, monkeypatch)
    message = unhealthy_apply(host, monkeypatch, image=OLD_IMAGE)
    assert "inspect it before any recovery" in message
    assert not (host.state / RELEASES[1] / "result.json").exists()
    assert p.unfinished(host.state) == [RELEASES[1]]
    rolled_back_refusal(host, "only the current applied release")


def test_an_unhealthy_release_is_not_rolled_back_after_another_release_became_current(tmp_path, monkeypatch):
    host = Host(tmp_path, monkeypatch)
    unhealthy_apply(host, monkeypatch)
    current = p.read(host.state / "current.json")
    p.write(host.state / "current.json", {**current, "applied_at": "2026-10-19T00:00:00Z"})
    rolled_back_refusal(host, "another release was applied or rolled back")


def settling_apply(host, monkeypatch, *states, settle=10):
    """Apply RELEASES[1] over RELEASES[0] with apply's settle (ANYM-758) on the fake clock. Once `up` passed its health
    wait, the replacement shows `states` in turn, one (State, RestartCount) per inspect (the last repeats). Returns the
    clock and the inspects of the replacement, or raises what apply raises."""
    host.release(RELEASES[0])
    host.main = RELEASES[1]
    p.prepare(host.settings, RELEASES[1])
    clock, original, inspects = Clock(), host.run, []
    monkeypatch.setattr(p, "time", clock)
    monkeypatch.setattr(p, "APPLY_SETTLE_SECONDS", settle)
    def run(argv, **kwargs):
        if argv[:2] == ["docker", "inspect"] and host.container["Image"] == host.image(RELEASES[1]):
            state, restarts = states[min(len(inspects), len(states) - 1)]
            host.container.update({"State": dict(state), "RestartCount": restarts})
            inspects.append(argv)
        return original(argv, **kwargs)
    monkeypatch.setattr(p, "run", run)
    try:
        p.apply(host.settings, RELEASES[1])
    finally:
        monkeypatch.setattr(p, "run", original)
    return clock, inspects


RUNNING, LOOPING = {"Running": True, "Status": "running"}, {"Running": True, "Restarting": True, "Status": "restarting"}


# spec: ANYM-758
@pytest.mark.parametrize("later", [(LOOPING, 1), ({"Running": True, "Status": "running"}, 2),
                                   ({"Running": False, "Status": "exited"}, 1)], ids=["restarting", "restarted", "exited"])
def test_a_replacement_that_loops_after_its_health_wait_is_recorded_unhealthy_with_rollback_data(tmp_path, monkeypatch,
                                                                                                   later):
    host = Host(tmp_path, monkeypatch)
    with pytest.raises(p.Refused, match="failed its health wait") as refused:
        settling_apply(host, monkeypatch, (RUNNING, 0), later)
    assert f"rollback --sha {RELEASES[1]}" in str(refused.value)
    assert "synthetic/api is restarting or stopped 10 s after it started" in str(refused.value.__cause__)
    result = p.read(host.state / RELEASES[1] / "result.json")
    assert result["status"] == "unhealthy" and [item["name"] for item in result["rollback"]["projects"]] == ["synthetic"]
    assert p.read(host.state / "current.json")["sha"] == RELEASES[0]
    p.rollback(host.settings, RELEASES[1])
    assert host.container["Image"] == host.image(RELEASES[0]) and host.container["State"]["Running"]


# spec: ANYM-758
def test_a_steady_replacement_passes_the_settle_with_exactly_one_more_inspect(tmp_path, monkeypatch):
    counts = []
    for settle in (0, 10):
        (tmp_path / str(settle)).mkdir()
        host = Host(tmp_path / str(settle), monkeypatch)
        clock, inspects = settling_apply(host, monkeypatch, (RUNNING, 0), settle=settle)
        assert p.read(host.state / "current.json")["sha"] == RELEASES[1] and "rollback-attempted.json" not in {
            path.name for path in (host.state / RELEASES[1]).iterdir()}
        assert clock.sleeps == ([10] if settle else [])
        counts.append(len(inspects))
    assert counts[1] == counts[0] + 1


# spec: ANYM-758
def test_a_replacement_with_a_compose_healthcheck_relies_on_its_health_wait_and_never_settles(tmp_path, monkeypatch):
    host = Host(tmp_path, monkeypatch)
    healthy = {**RUNNING, "Health": {"Status": "healthy"}}
    clock, _ = settling_apply(host, monkeypatch, (healthy, 0), (LOOPING, 3))
    assert clock.sleeps == [] and p.read(host.state / "current.json")["sha"] == RELEASES[1]


# spec: ANYM-758
def test_the_settle_sleeps_once_per_project_and_leaves_the_call_wait_alone(tmp_path, monkeypatch):
    # A call starts after the first project settled and before the second project's stop.
    host = CallHost(tmp_path, monkeypatch, answers=((), (), [(CALL, "active")], ()))
    monkeypatch.setattr(p, "APPLY_SETTLE_SECONDS", 10)
    host.prepared(RELEASES[0])
    assert p.apply(host.settings, RELEASES[0])["status"] == "applied"
    # One settle per project, the unchanged call poll between them.
    assert len(host.settings["projects"]) == 2 and host.clock.sleeps == [10, p.CALL_POLL, 10]
    voice = host.events.index(("stop", "personal-os-voice"))
    assert [event for event in host.events[:voice] if event[0] == "probe"][-2:] == [
        ("probe", "voice0", [f"{RELEASES[0]}/attempted.json"])] * 2  # the active probe, then the one that let it stop


# spec: ANYM-758
def test_the_settle_of_a_project_with_several_services_sleeps_once_and_inspects_each(tmp_path, monkeypatch):
    clock, inspected = Clock(), []
    monkeypatch.setattr(p, "time", clock)
    monkeypatch.setattr(p, "APPLY_SETTLE_SECONDS", 10)
    def run(argv, **kwargs):
        assert argv[:2] == ["docker", "inspect"] and clock.sleeps == [10]  # every inspect after the one sleep
        inspected.append(argv[2])
        return json.dumps([{"Id": argv[2], "State": {"Running": True}, "RestartCount": 0}])
    monkeypatch.setattr(p, "run", run)
    project = {"name": "workers", "services": {"indexer": "runtime", "enricher": "runtime", "sender": "runtime"}}
    item = {"running": {service: {"container": "old-" + service} for service in project["services"]}}
    unproven = [(service, "new-" + service, {"State": {"Running": True}, "RestartCount": 0}) for service in project["services"]]
    assert p.restart_loop(project, item, unproven) is None
    assert clock.sleeps == [10] and inspected == ["new-indexer", "new-enricher", "new-sender"]
    # The second of three loops: one sleep still, the first two inspected, and its name in the reason.
    clock.sleeps.clear()
    inspected.clear()
    def looping(argv, **kwargs):
        inspected.append(argv[2])
        return json.dumps([{"Id": argv[2], "State": {"Running": True}, "RestartCount": 1 if argv[2] == "new-enricher" else 0}])
    monkeypatch.setattr(p, "run", looping)
    assert "workers/enricher is restarting or stopped 10 s" in p.restart_loop(project, item, unproven)
    assert clock.sleeps == [10] and inspected == ["new-indexer", "new-enricher"]


def inventory(*versions, changed=()):
    """A synthetic image schema inventory: schema.sql and the migrations of these numbers, `changed` ones with another
    checksum."""
    value = {"schema.sql": hashlib.sha256(b"schema").hexdigest()}
    for version in versions:
        name = f"migrations/{version:03d}_synthetic.sql"
        value[name] = hashlib.sha256((name + ("changed" if version in changed else "")).encode()).hexdigest()
    return value


APPLIED = inventory(*range(1, 61))


class PlanB:
    """Plan B beside a synthetic plan A `Host`: an HTTP configuration file, its state serving one generation, the local
    source mirror (main is RELEASES in order) and the image inventories local inspection reads. No Docker or git."""
    GENERATION = "abcdef123456"

    def __init__(self, host, monkeypatch, sha, image, inventories):
        from scripts import seamless_release as s
        self.host, self.s, self.inventories, self.reads = host, s, dict(inventories), []
        self.state, self.file = host.root / "http-state", host.root / "http.json"
        self.state.mkdir(mode=0o700)
        (host.root / "global").mkdir(mode=0o700)
        (host.state / "deploy-source.git").mkdir()
        self.http = {"version": 1, "state_directory": str(self.state), "projects": host.settings["projects"]}
        self.file.write_text(json.dumps(self.http))
        monkeypatch.setattr(s, "GLOBAL_LOCK", host.root / "global")
        monkeypatch.setattr(s, "validate_settings", lambda value: value)
        monkeypatch.setattr(p, "config", lambda path: self.http if str(path) == str(self.file) else host.settings)
        monkeypatch.setattr(p, "HTTP_CONFIG", str(self.file))
        monkeypatch.setattr(p, "run", self.run)
        self.serve(sha, image)

    def serve(self, sha, image):
        """Promote's receipts for `sha` serving `image`: the attempt, the release's result and http-current.json."""
        (self.state / sha).mkdir(exist_ok=True)
        receipt = {"sha": sha, "generation": self.GENERATION, "status": "serving",
                   "images": {"runtime": image, "gateway": IMAGE}, "schema": self.inventories.get(image, {}),
                   "containers": {"api": {"container": "api0", "image": image},
                                  "gateway": {"container": "gateway0", "image": IMAGE}}}
        p.write(self.state / sha / "http-attempted.json", {"status": "serving", "sha": sha})
        p.write(self.state / sha / "http-result.json", receipt)
        p.write(self.state / "http-current.json", receipt)

    def run(self, argv, **kwargs):
        if argv[:2] == ["docker", "run"] and self.s.SCHEMA_READER in argv:
            self.reads.append(argv)
            image = argv[argv.index("--entrypoint") + 2]
            if image not in self.inventories:
                raise p.Refused("docker failed (exit 125)")
            return json.dumps(self.inventories[image])
        if argv[0] == "git" and "merge-base" in argv:
            ancestor, sha = argv[-2:]
            if ancestor in RELEASES and sha in RELEASES and RELEASES.index(ancestor) <= RELEASES.index(sha):
                return ""
            raise p.Refused("git failed (exit 1)")
        return self.host.run(argv, **kwargs)

    def refused(self, match, sha=RELEASES[1], http=True):
        """A rollback of `sha` refused before any journal, stop or `up`."""
        self.host.calls.clear()
        with pytest.raises(p.Refused, match=match) as refusal:
            p.rollback(self.host.settings, sha, self.http if http else None)
        assert self.host.mutations() == [] and not (self.host.state / sha / "rollback-attempted.json").exists()
        return str(refusal.value)


def promoted(tmp_path, monkeypatch, serving, restored=APPLIED):
    """RELEASES[0] and then RELEASES[1] applied by plan A, and plan B serving RELEASES[1]'s runtime image with the
    inventory `serving`; RELEASES[0]'s image, the one a rollback restores, has `restored`."""
    host = Host(tmp_path, monkeypatch)
    for sha in RELEASES[:2]:
        host.release(sha)
    plan = PlanB(host, monkeypatch, RELEASES[1], host.image(RELEASES[1]),
                 {host.image(RELEASES[1]): serving, host.image(RELEASES[0]): restored})
    return host, plan


# spec: ANYM-642
@pytest.mark.parametrize("case", ["malformed", "missing", "not serving", "attempt not serving", "unresolved attempt",
                                  "unpublished attempt", "no mirror", "state without configuration",
                                  "container image", "result image", "no result", "recorded schema",
                                  "configured state without configuration", "deploy record without configuration",
                                  "dangling link without configuration", "dangling sibling without configuration"])
def test_rollback_refuses_unresolved_or_malformed_http_state(tmp_path, monkeypatch, case):
    host, plan = promoted(tmp_path, monkeypatch, APPLIED)
    current = plan.state / "http-current.json"
    match = {"malformed": "cannot be read: JSONDecodeError", "missing": "plan B's state is unknown",
             "not serving": "inconsistent: .* names no serving generation", "attempt not serving": "does not serve",
             "unresolved attempt": "an HTTP release attempt is unresolved: " + RELEASES[2],
             "unpublished attempt": "an HTTP release attempt serves, but http-current.json does not name it: "
                                    + RELEASES[2], "no mirror": "publication cannot be checked",
             "state without configuration": "there is no HTTP configuration, but HTTP state exists",
             "container image": "records name different runtime images",
             "result image": "records name different runtime images", "no result": "cannot be read: FileNotFoundError",
             "recorded schema": "the schema recorded for generation " + PlanB.GENERATION + " is not its runtime",
             "configured state without configuration": "HTTP state exists: " + str(tmp_path / "sibling"),
             "deploy record without configuration": "a deploy promoted plan B",
             "dangling link without configuration": "HTTP state exists: " + str(host.state / "http-current.json"),
             "dangling sibling without configuration": "HTTP state exists: " + str(p.HOST_STATE / "plan-b")}[case]
    result = plan.state / RELEASES[1] / "http-result.json"
    if case == "malformed":
        current.write_text("not json")
    elif case == "missing":
        current.unlink()
    elif case == "not serving":
        p.write(current, {**p.read(current), "status": "recovery-required"})
    elif case == "attempt not serving":
        p.write(plan.state / RELEASES[1] / "http-attempted.json", {"status": "switching", "sha": RELEASES[1]})
    elif case in ("unresolved attempt", "unpublished attempt"):
        (plan.state / RELEASES[2]).mkdir()
        p.write(plan.state / RELEASES[2] / "http-attempted.json",
                {"status": "switching" if case == "unresolved attempt" else "serving", "sha": RELEASES[2]})
    elif case == "no mirror":
        (host.state / "deploy-source.git").rmdir()
    elif case == "state without configuration":
        (host.state / "http").mkdir()
    elif case == "container image":
        # The receipt's runtime image names the previous release, its own container record the newer one.
        p.write(current, {**p.read(current), "images": {"runtime": host.image(RELEASES[0]), "gateway": IMAGE},
                          "schema": APPLIED})
    elif case == "result image":
        p.write(result, {**p.read(result), "images": {"runtime": host.image(RELEASES[0]), "gateway": IMAGE}})
    elif case == "no result":
        result.unlink()
    elif case == "recorded schema":
        p.write(current, {**p.read(current), "schema": inventory(*range(1, 62))})
    elif case == "configured state without configuration":
        # A custom HTTP configuration elsewhere: its state, a sibling of plan A's, is named by an /etc/deedkit config.
        (tmp_path / "sibling").mkdir()
        p.write(tmp_path / "sibling" / "http-current.json", p.read(current))
        p.CONFIG_DIRECTORY.mkdir(parents=True)
        p.write(p.CONFIG_DIRECTORY / "http-custom.json", {"state_directory": str(tmp_path / "sibling")})
    elif case == "deploy record without configuration":
        p.write(host.state / "deploy.json", {"sha": RELEASES[1], "status": "deployed", "http": "serving"})
    elif case == "dangling link without configuration":
        # A link whose target is gone is still a marker, never taken for no plan B.
        (host.state / "http-current.json").symlink_to(tmp_path / "gone" / "http-current.json")
    else:
        # So is one in a sibling state under the host state root that holds nothing else.
        (p.HOST_STATE / "plan-b").mkdir(parents=True)
        (p.HOST_STATE / "plan-b" / "http-current.json").symlink_to(tmp_path / "gone" / "http-current.json")
    # The cases without configuration pass none; plan B's own state directory is not under plan A's.
    plan.refused(match, http="without configuration" not in case)
    # Refused before any image inventory was read (http-current.json is not trusted), but for the recorded schema,
    # which only the serving image's inventory contradicts.
    assert plan.reads == [] or case == "recorded schema"


# spec: ANYM-642
def test_rollback_without_http_configuration_passes(tmp_path, monkeypatch, capsys):
    host = Host(tmp_path, monkeypatch)
    for sha in RELEASES[:2]:
        host.release(sha)
    monkeypatch.setattr(p, "config", lambda path: host.settings)
    monkeypatch.setattr(p, "HTTP_CONFIG", str(tmp_path / "absent" / "http-production.json"))
    host.calls.clear()
    p.main(["rollback", "--sha", RELEASES[1]])
    assert json.loads(capsys.readouterr().out)["previous_images"] == {"synthetic": {"api": host.image(RELEASES[0])}}
    assert host.container["Image"] == host.image(RELEASES[0])
    from scripts import seamless_release as s
    assert not any(s.SCHEMA_READER in call for call in host.calls)


# spec: ANYM-642
def test_rollback_holds_http_locks(tmp_path, monkeypatch, capsys):
    host, plan = promoted(tmp_path, monkeypatch, APPLIED)
    held = []
    def run(argv, **kwargs):
        if argv[:2] == ["docker", "compose"] and "stop" in argv:
            for directory in (plan.s.GLOBAL_LOCK, plan.state, host.state):
                with pytest.raises(p.Busy), contextlib.ExitStack() as stack:
                    stack.enter_context(p.lock(directory))
                held.append(directory)
        return plan.run(argv, **kwargs)
    monkeypatch.setattr(p, "run", run)
    p.main(["rollback", "--sha", RELEASES[1]])
    assert json.loads(capsys.readouterr().out)["status"] == "rolled-back"
    assert held == [plan.s.GLOBAL_LOCK, plan.state, host.state] and plan.reads


# spec: ANYM-642
@pytest.mark.parametrize("outcome", ["equal", "older-serving prefix", "newer-serving", "divergent", "not a prefix",
                                     "unreadable", "no messaging image"])
def test_rollback_guard_outcomes(tmp_path, monkeypatch, outcome):
    serving = {"equal": APPLIED, "older-serving prefix": inventory(*range(1, 60)),
               "newer-serving": inventory(*range(1, 62)), "divergent": inventory(*range(1, 61), changed={60}),
               "not a prefix": inventory(*range(1, 60), 61), "unreadable": APPLIED,
               "no messaging image": inventory(*range(1, 62))}[outcome]
    host, plan = promoted(tmp_path, monkeypatch, serving)
    restored = host.image(RELEASES[0])
    if outcome in ("unreadable", "no messaging image"):
        del plan.inventories[restored]
    if outcome == "no messaging image":
        # A rollback that restores no messaging image (a calls-only plan) never inspects one, even with plan B newer
        # and its inventory unreadable.
        plan.inventories.clear()
        serving = p.serving_generation(host.settings, plan.http)
        assert p.guard_rollback(serving, [({"name": "voice", "services": {"calls": "calls"}}, None,
                                           {"calls": restored})]) is None and plan.reads == []
        return
    if outcome in ("equal", "older-serving prefix"):
        result = p.rollback(host.settings, RELEASES[1], plan.http)
        assert result["previous_images"] == {"synthetic": {"api": restored}} and host.container["Image"] == restored
    else:
        message = plan.refused("plan B serves generation " + PlanB.GENERATION + " of " + RELEASES[1])
        if outcome == "unreadable":
            assert f"the schema inventory of {restored} cannot be read (docker failed (exit 125))" in message
            assert "Roll forward instead: `production.py deploy` of a newer green main SHA" in message
        else:
            where = f"synthetic/api's restored image {restored}"
            assert {"newer-serving": f"with migrations {where} lacks: migrations/061_synthetic.sql;",
                    "divergent": f"inventory {where} diverges from (changed: migrations/060_synthetic.sql; missing: none)",
                    "not a prefix": f"inventory {where} diverges from (changed: none; missing: migrations/061_synthetic"
                                    ".sql)"}[outcome] in message
            assert ("a plan A rollback alone would leave the serving API newer than its workers. Roll forward instead: "
                    "`production.py deploy` of a newer green main SHA") in message
        assert host.container["Image"] == host.image(RELEASES[1]) and host.container["State"]["Running"]
    # Local image inspection of exactly the serving and the restored image: no network, read-only.
    assert {argv[argv.index("--entrypoint") + 2] for argv in plan.reads} == {host.image(RELEASES[1]), restored}
    assert all(argv[3:5] == ["--network", "none"] and "--read-only" in argv for argv in plan.reads)


# spec: ANYM-642
def test_rollback_guard_refuses_newer_serving(tmp_path, monkeypatch, capsys):
    host, plan = promoted(tmp_path, monkeypatch, inventory(*range(1, 62)))
    host.calls.clear()
    with pytest.raises(SystemExit):
        p.main(["rollback", "--sha", RELEASES[1]])
    error = capsys.readouterr().err
    assert "promotion refused: plan B serves generation " + PlanB.GENERATION in error
    assert "migrations/061_synthetic.sql" in error and "production.py deploy" in error
    assert host.mutations() == [] and not (host.state / RELEASES[1] / "rollback-attempted.json").exists()
    assert host.container["Image"] == host.image(RELEASES[1])


# spec: ANYM-642
@pytest.mark.parametrize("serving", ["equal", "older"])
def test_unhealthy_apply_rollback_passes_guard(tmp_path, monkeypatch, serving):
    host = Host(tmp_path, monkeypatch)
    unhealthy_apply(host, monkeypatch)
    # Plan B still serves the base release, or an older generation whose inventory is a prefix of the base's.
    image = host.image(RELEASES[0]) if serving == "equal" else OLD_IMAGE
    plan = PlanB(host, monkeypatch, RELEASES[0], image,
                 {host.image(RELEASES[0]): APPLIED, OLD_IMAGE: inventory(*range(1, 60))})
    assert p.rollback(host.settings, RELEASES[1], plan.http)["status"] == "rolled-back"
    assert host.container["Image"] == host.image(RELEASES[0]) and plan.reads
    # deploy's automatic rollback hands it the HTTP configuration, so the same guard runs there.
    (tmp_path / "deploy").mkdir()
    deployer, seen = Deployer(tmp_path / "deploy", monkeypatch), []
    def failed(sha):
        p.write(deployer.state / sha / "result.json", {"sha": sha, "status": "unhealthy"})
        raise p.Refused("a replacement failed its health wait")
    deployer.fail_apply = failed
    monkeypatch.setattr(p, "rollback", lambda settings, sha, http=None: seen.append((sha, http)) or {"sha": sha})
    assert deployer.deploy()["status"] == "rolled-back" and seen == [(NEWEST, deployer.http)]


# spec: ANYM-642
def test_retry_after_guard_refusal(tmp_path, monkeypatch):
    host, plan = promoted(tmp_path, monkeypatch, inventory(*range(1, 62)))
    plan.refused("migrations/061_synthetic.sql")
    # The refusal changed nothing: once plan B serves a generation the restored workers fit, the same rollback runs.
    plan.inventories[IMAGE] = APPLIED
    plan.serve(RELEASES[2], IMAGE)
    assert p.rollback(host.settings, RELEASES[1], plan.http)["status"] == "rolled-back"
    assert host.container["Image"] == host.image(RELEASES[0])


# spec: ANYM-642
def test_partially_executed_rollback_keeps_refusing(tmp_path, monkeypatch):
    host, plan = promoted(tmp_path, monkeypatch, inventory(*range(1, 62)))
    p.write(host.state / RELEASES[1] / "rollback-attempted.json", {"sha": RELEASES[1], "status": "rolling-back"})
    host.calls.clear()
    with pytest.raises(p.Refused, match="already rolled back or attempted"):
        p.rollback(host.settings, RELEASES[1], plan.http)
    assert host.mutations() == []
    # A journal removed by hand never bypasses the guard: it runs again on every invocation before any change.
    (host.state / RELEASES[1] / "rollback-attempted.json").unlink()
    plan.refused("plan B serves generation .* lacks: migrations/061_synthetic.sql")


# spec: ANYM-642
def test_roll_forward_after_guard_refusal(tmp_path, monkeypatch):
    host, plan = promoted(tmp_path, monkeypatch, inventory(*range(1, 62)))
    plan.refused("Roll forward instead")
    assert (host.state / RELEASES[1] / "attempted.json").exists()
    # A newer synthetic SHA (a fix or a revert keeping every migration): its own attempt marker, the same schema, and
    # plan A applies it without the guard.
    plan.reads.clear()
    host.release(RELEASES[2])
    current = p.read(host.state / "current.json")
    assert current["sha"] == RELEASES[2] and current["status"] == "applied"
    assert host.container["Image"] == host.image(RELEASES[2]) and plan.reads == []


BASE, NEWER, NEWEST = "0" * 40, "1" * 40, "2" * 40


class Deployer:
    """A synthetic host for `deploy`: plan A and HTTP state at BASE, a main history BASE..NEWEST, the commits whose
    images are attested, and the release steps recorded instead of run. No git (but the check of an existing source
    mirror), GitHub, registry or Docker access."""
    real_run = staticmethod(p.run)

    def __init__(self, tmp_path, monkeypatch, *, history=(NEWER, NEWEST), green=(NEWER, NEWEST), changes=None):
        from scripts import seamless_release as s
        self.s, self.root = s, tmp_path
        self.state, self.http_state = tmp_path / "state", tmp_path / "http-state"
        for directory in (self.state, self.http_state, tmp_path / "global"):
            directory.mkdir(mode=0o700)
        self.document = {"name": "synthetic", "services": {"api": {"image": "old"}}}
        project = {"name": "synthetic", "directory": str(tmp_path), "files": [str(tmp_path / "compose.json")],
                   "services": {"api": "runtime"}}
        self.settings = {"version": 1, "state_directory": str(self.state), "projects": [project]}
        self.http = {"version": 1, "state_directory": str(self.http_state), "projects": [project], "http_release": {}}
        self.history, self.changes = [BASE, *history], changes or {}
        self.green, self.unreached = set(green), set()
        self.steps, self.requests, self.git = [], [], []
        self.blobs, self.git_failures, self.contents, self.modified = {}, {}, {}, set()
        self.current(BASE)
        p.write(self.http_state / "http-current.json",
                {"sha": BASE, "status": "serving", "config_digest": p.digest(self.http)})
        monkeypatch.setattr(p, "PAUSE", tmp_path / "autodeploy.pause")
        monkeypatch.setattr(s, "GLOBAL_LOCK", tmp_path / "global")
        monkeypatch.setattr(p, "run", self.run)
        def offline(*args, **kwargs):
            raise AssertionError("deploy made a GitHub API request")
        monkeypatch.setattr(p.urllib.request, "urlopen", offline)
        for module, name in ((p, "prepare"), (p, "apply"), (p, "rollback"), (s, "prepare"), (s, "promote"),
                             (s, "retire")):
            monkeypatch.setattr(module, name, self.step(module is s, name))
        self.retained, self.preflight = [], []
        def retain(settings, *, held=(), **extra):
            # ANYM-689: the preflight pass before the disk check passes a deadline; the post-release pass never does.
            (self.preflight if extra else self.retained).append(held)
            return {"removed": [], "failed": [], "kept": 2}
        monkeypatch.setattr(p, "retain_images", retain)

    def current(self, sha):
        p.write(self.state / "current.json", {"sha": sha, "status": "applied", "ci": {"url": "https://example.invalid"},
                                              "rollback": {"version": 1, "projects": [
                                                  {"name": project["name"], "after_digest": p.digest(self.document)}
                                                  for project in self.settings["projects"]]}})
        (self.state / sha).mkdir(exist_ok=True)
        p.write(self.state / sha / "prepared.json", {"sha": sha, "config_digest": p.digest(self.settings)})

    def step(self, http, name):
        label = ("seamless " if http else "") + name
        def call(settings, sha, *rest):
            self.steps.append((label, sha))
            failure = getattr(self, "fail_" + label.replace(" ", "_"), None)
            if label == "prepare":
                (self.state / sha).mkdir()
                p.write(self.state / sha / "prepared.json", {"sha": sha, "config_digest": p.digest(self.settings)})
            if label == "apply":
                p.write(self.state / sha / "attempted.json", {"sha": sha})
            if label == "seamless prepare":
                (self.http_state / sha).mkdir()
            if failure:
                failure(sha)
            if label == "apply":
                p.write(self.state / sha / "result.json", {"sha": sha, "status": "applied"})
                self.current(sha)
            if label == "seamless prepare":
                p.write(self.http_state / sha / "http-prepared.json", {"sha": sha})
            if label == "seamless promote":
                p.write(self.http_state / sha / "http-attempted.json", {"status": "serving"})
                p.write(self.http_state / "http-current.json",
                        {"sha": sha, "status": "serving", "config_digest": p.digest(self.http)})
                return {"sha": sha, "status": "serving"}
            return {"sha": sha}
        return call

    def attested(self, sha):
        """The verifier's answer for the proof image of `sha`: attested only once its CI published it."""
        self.requests.append(sha)
        if sha in self.unreached:
            raise p.Unreachable("attest-verify failed (exit 1): attest-verify: refused: registry request failed: timeout")
        if sha not in self.green:
            raise p.Unattested("attest-verify failed (exit 1): attest-verify: no attestation")
        return {"digest": IMAGE}

    def run(self, argv, **kwargs):
        if argv[:2] == ["docker", "compose"] and "config" in argv:
            return json.dumps(self.document)
        assert argv[0] == "git", argv
        self.git.append(argv)
        if "--is-bare-repository" in argv:
            return self.real_run(argv, **kwargs)
        if isinstance(failure := self.git_failures.get(argv[1]), str):
            # git's stderr, through the real run(): a synthetic process prints it and exits 128.
            return self.real_run([sys.executable, "-c", f"import sys; sys.stderr.write({failure!r}); sys.exit(128)"],
                                 **kwargs)
        if failure:
            raise failure
        head = self.history[-1]
        if argv[1] == "ls-remote":
            return f"{head}\trefs/heads/main\n"
        command = argv[3:]
        if command[:1] == ["rev-parse"]:
            if command[1] == "refs/heads/main":
                return head + "\n"
            return self.blobs.get(command[1], "e" * 40) + "\n"
        if command[:2] == ["merge-base", "--is-ancestor"]:
            if self.history.index(command[2]) > self.history.index(command[3]):
                raise p.Refused("git failed (exit 1); production was not accepted")
            return ""
        if command[:2] == ["rev-list", "--first-parent"]:
            old, new = command[2].split("..")
            return "\n".join(self.history[self.history.index(old) + 1:self.history.index(new) + 1][::-1])
        if command[:1] == ["diff"]:
            old, new = command[-2:]
            commits = self.history[self.history.index(old) + 1:self.history.index(new) + 1]
            return "\0".join(path for sha in commits for path in self.changes.get(sha, ())
                             if "--diff-filter=A" not in command or path not in self.modified) + "\0"
        if command[:2] == ["cat-file", "blob"] and command[2] in self.contents:
            return self.contents[command[2]]
        if command[:2] == ["cat-file", "-s"] and command[2] in self.contents:
            return f"{len(self.contents[command[2]])}\n"
        if command[:2] in (["cat-file", "blob"], ["cat-file", "-s"]):
            raise p.Refused("git failed (exit 128); production was not accepted")
        if command[:1] == ["ls-tree"]:  # ANYM-745: the files of a directory at a commit, from `contents`
            prefix = f"{command[-2]}:{command[-1]}"
            return "\0".join(key.split(":", 1)[1] for key in self.contents if key.startswith(prefix)) + "\0"
        return ""  # clone and fetch of the blobless mirror

    def deploy(self, **options):
        return p.deploy(self.settings, self.http, attested=self.attested, **options)

    def paused(self):
        return json.loads(p.PAUSE.read_text())["reason"] if p.PAUSE.exists() else None


def test_deploy_applies_the_newest_green_main_commit_with_plan_a_then_plan_b(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch, history=(NEWER, NEWEST, "3" * 40), green=(NEWER, NEWEST))
    outcome = host.deploy()
    # The newest commit's CI still runs: the newest green one is deployed, everything at one SHA.
    assert host.steps == [("prepare", NEWEST), ("apply", NEWEST), ("seamless prepare", NEWEST),
                          ("seamless promote", NEWEST)]
    assert outcome["status"] == "deployed" and outcome["sha"] == NEWEST and outcome["base"] == BASE
    assert "run" not in outcome and outcome["http"] == "serving"
    assert p.read(host.state / "deploy.json") == outcome and host.paused() is None
    assert host.requests == ["3" * 40, NEWEST]  # newest first, until one is attested
    # The next run waits for the newest commit's CI: it is not attested yet.
    host.requests.clear()
    assert host.deploy()["status"] == "waiting" and host.requests == ["3" * 40] and len(host.steps) == 4


def test_deploy_without_a_new_main_commit_reads_neither_the_api_nor_the_history(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch, history=())
    assert host.deploy() == {"status": "current", "sha": BASE}
    assert host.requests == [] and [argv[1] for argv in host.git] == ["ls-remote"] and host.steps == []


def test_deploy_exits_quietly_while_paused_or_while_another_operation_holds_the_lock(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    p.PAUSE.write_text("owner: synthetic maintenance\n")
    assert host.deploy() == {"status": "paused", "reason": "owner: synthetic maintenance"}
    p.PAUSE.unlink()
    with p.lock(host.state):
        assert host.deploy()["status"] == "busy"
    assert host.steps == [] and host.git == [] and host.paused() is None


def test_a_host_lock_refused_for_another_reason_than_a_concurrent_operation_pauses(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    host.state.chmod(0o750)  # lock() refuses a state directory that is not private
    outcome = host.deploy()
    assert outcome["status"] == "halted" and "could not take the host locks: production state directory must be private" \
        in outcome["reason"]
    assert host.paused() == outcome["reason"] and host.steps == [] and host.git == []


@pytest.mark.parametrize("path", ["services/messaging/messaging_service/migrations/036_synthetic.sql",
                                  "services/calendar/family_calendar/service/schema.sql",
                                  "services/messaging/messaging_service/gateway_role.sql",
                                  "scripts/production.py", "scripts/seamless_release.py",
                                  "tools/attest-verify/main.go", "services/release-router/main.go"])
def test_deploy_pauses_for_an_sql_inventory_or_deploy_tool_change_before_any_step(tmp_path, monkeypatch, path):
    host = Deployer(tmp_path, monkeypatch, changes={NEWER: [path, "README.md"]})
    outcome = host.deploy()
    assert outcome["status"] == "halted" and path in outcome["reason"] and outcome["sha"] == NEWEST
    assert host.steps == [] and host.paused() == outcome["reason"]
    assert host.deploy()["status"] == "paused"


def test_documentation_about_migrations_is_not_an_sql_inventory_change(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch, changes={NEWER: ["guide/migrations/synthetic-layout.md",
                                                            "services/messaging/migration-notes.md"]})
    assert host.deploy()["status"] == "deployed"


# spec: ANYM-914
def test_after_claims_ran_a_deploy_to_a_tree_without_session_claims_is_refused_before_any_step_unless_a_reason_is_given(
        tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch, changes={NEWER: ["scripts/production.py"]})
    p.write(host.state / p.CLAIM_RECORD, {"sha": BASE, "recorded_at": "2026-11-09T00:00:00Z"})
    outcome = host.deploy()
    assert outcome["status"] == "halted" and outcome["code"] == "deploy_refused"
    assert "pre_claim_rollback_refused" in outcome["reason"] and host.steps == []  # not even the tool update
    p.PAUSE.unlink()
    host.changes = {}
    outcome = host.deploy(pre_claim="synthetic: one host runs sessions")
    assert outcome["status"] == "deployed" and outcome["pre_claim_rollback"] == "synthetic: one host runs sessions"
    assert p.read(host.state / "deploy.json")["pre_claim_rollback"] == "synthetic: one host runs sessions"


# spec: ANYM-914
def test_a_deploy_to_a_tree_that_carries_session_claims_needs_no_reason(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    p.write(host.state / p.CLAIM_RECORD, {"sha": BASE, "recorded_at": "2026-11-09T00:00:00Z"})
    host.contents[f"{NEWEST}:{p.SESSION_CLAIM}"] = "CLAIM = 1\n"
    outcome = host.deploy()
    assert outcome["status"] == "deployed" and "pre_claim_rollback" not in outcome


MIGRATION_DIRECTORY = "services/messaging/messaging_service/migrations/"
ROLLOUT = ROOT / "services/messaging/messaging_service/migration_rollout.py"


def migration_host(tmp_path, monkeypatch, files, *, classifier=None, others=()):
    """A range NEWER..NEWEST that adds `files` (name: bytes) and changes `others`; the mirror holds their bytes."""
    host = Deployer(tmp_path, monkeypatch, changes={NEWER: [MIGRATION_DIRECTORY + name for name in files] + ["README.md"],
                                                    NEWEST: list(others)})
    host.contents = {f"{NEWEST}:{MIGRATION_DIRECTORY}{name}": data for name, data in files.items()}
    host.contents[f"{NEWEST}:{p.CLASSIFIER}"] = ROLLOUT.read_bytes() if classifier is None else classifier
    return host


# spec: ANYM-444
def test_the_sql_pause_names_the_class_the_candidate_classifier_gives_each_added_migration(tmp_path, monkeypatch):
    files = {"041_synthetic_expand.sql": b"-- rollout: expand\nALTER TABLE messages ADD COLUMN synthetic_note text;\n",
             "042_synthetic_manual.sql": b"-- rollout: manual\nINSERT INTO probe_metadata VALUES ('marker-7f3a','x');\n",
             "043_synthetic_claim.sql": b"-- rollout: expand\nUPDATE messages SET body='marker-9c1e';\n",
             "044_synthetic_bare.sql": b"ALTER TABLE messages ADD COLUMN synthetic_rank integer;\n"}
    edited = MIGRATION_DIRECTORY + "039_feedback_status.sql"
    host = migration_host(tmp_path, monkeypatch, files, others=[edited])
    host.modified = {edited}
    outcome = host.deploy()
    paths = ", ".join([MIGRATION_DIRECTORY + name for name in files] + [edited])
    # The pause is the one before ANYM-444; its reason adds only file names and classes, never file contents.
    assert outcome["status"] == "halted" and outcome["sha"] == NEWEST and host.steps == []
    assert outcome["reason"] == (f"the SQL inventory changed ({paths}); migrate by hand (production.md); added "
                                 "migrations: 041_synthetic_expand.sql expand non-critical, 042_synthetic_manual.sql "
                                 "manual non-critical, 043_synthetic_claim.sql manual critical: line 2 UPDATE, "
                                 "044_synthetic_bare.sql no header non-critical")
    assert host.paused() == outcome["reason"] and "marker" not in outcome["reason"]
    assert [argv[3:] for argv in host.git if argv[3:5] == ["cat-file", "blob"]][-1] == \
        ["cat-file", "blob", f"{NEWEST}:{p.CLASSIFIER}"]
    assert p.pause_reasons(host.state, BASE, NEWEST, {}) == [outcome["reason"]]


# spec: ANYM-444
@pytest.mark.parametrize("path", ["services/calendar/family_calendar/service/schema.sql",
                                  "services/messaging/messaging_service/gateway_role.sql",
                                  "services/messaging/messaging_service/schema.sql"])
def test_a_schema_or_role_file_pauses_exactly_as_before(tmp_path, monkeypatch, path):
    host = migration_host(tmp_path, monkeypatch, {}, others=[path])
    outcome = host.deploy()
    assert outcome["status"] == "halted" and host.steps == [] and host.paused() == outcome["reason"]
    assert outcome["reason"] == f"the SQL inventory changed ({path}); migrate by hand (production.md)"
    assert not [argv for argv in host.git if "cat-file" in argv or "--diff-filter=A" in argv]


# spec: ANYM-444
@pytest.mark.parametrize("classifier", [b"raise RuntimeError('synthetic')\n",
                                        b"def rollout_class(text):\n    return open('/etc/hosts').read()\n",
                                        b"def rollout_class(text):\n    return 'expand; deploy anyway'\n",
                                        b"import json.tool\ndef rollout_class(text):\n    return 'expand'\n", None],
                         ids=["raises", "opens-a-file", "other-text", "imports", "missing"])
def test_a_failing_classifier_names_the_migrations_manual_and_pauses_the_same(tmp_path, monkeypatch, classifier):
    files = {"041_synthetic_expand.sql": b"-- rollout: expand\nALTER TABLE messages ADD COLUMN synthetic_note text;\n"}
    host = migration_host(tmp_path, monkeypatch, files, classifier=classifier)
    if classifier is None:
        del host.contents[f"{NEWEST}:{p.CLASSIFIER}"]
    outcome = host.deploy()
    assert outcome["status"] == "halted" and host.steps == [] and host.paused() == outcome["reason"]
    assert outcome["reason"].endswith("; added migrations: 041_synthetic_expand.sql manual")


ONE_EXPAND = {"041_synthetic_expand.sql": b"-- rollout: expand\nALTER TABLE messages ADD COLUMN synthetic_note text;\n"}
NO_ADDRESS_LIMIT = sys.platform == "darwin" or not hasattr(resource, "RLIMIT_AS")


def classified(tmp_path, monkeypatch, files, classifier):
    """The pause reason of a range adding `files`, classified by `classifier`, and the argv of each process run."""
    host, started, real = migration_host(tmp_path, monkeypatch, files, classifier=classifier), [], subprocess.run
    def recording(argv, *args, **kwargs):
        started.append(argv)
        return real(argv, *args, **kwargs)
    monkeypatch.setattr(p.subprocess, "run", recording)
    outcome = host.deploy()
    assert outcome["status"] == "halted" and host.steps == [] and host.paused() == outcome["reason"]
    assert outcome["reason"].startswith("the SQL inventory changed (") and "; migrate by hand (production.md)" in \
        outcome["reason"]
    return outcome["reason"], host, [argv for argv in started if p.CLASSIFY in argv]


# spec: ANYM-444
@pytest.mark.parametrize("classifier", [
    b"import sys\nsys.stdout.write(' ' * (1 << 20))\ndef rollout_class(text):\n    return 'expand'\n",
    b"import sys\nsys.stdout.write('[' * 10000 + ']' * 10000)\nsys.stdout.flush()\nraise SystemExit(0)\n",
    pytest.param(b"def rollout_class(text):\n    return bytearray(1 << 30) and 'expand'\n",
                 marks=pytest.mark.skipif(NO_ADDRESS_LIMIT, reason="RLIMIT_AS is not enforced on this platform"))],
    ids=["huge-stdout", "nested-json", "memory-hog"])
def test_a_classifier_beyond_its_output_or_memory_bound_names_the_migrations_manual(tmp_path, monkeypatch, classifier):
    reason, _, started = classified(tmp_path, monkeypatch, ONE_EXPAND, classifier)
    assert reason.endswith("; added migrations: 041_synthetic_expand.sql manual") and len(started) == 1


# spec: ANYM-444
def test_migrations_over_the_input_bound_are_manual_and_no_classifier_starts(tmp_path, monkeypatch):
    files = dict(ONE_EXPAND)
    files["042_synthetic_large.sql"] = b"-- rollout: expand\n" + b"-- synthetic padding\n" * (p.CLASSIFY_INPUT_LIMIT // 21)
    reason, host, started = classified(tmp_path, monkeypatch, files, None)
    assert reason.endswith("; added migrations: 041_synthetic_expand.sql manual, 042_synthetic_large.sql manual")
    assert started == [] and not [argv for argv in host.git if argv[3:5] == ["cat-file", "blob"]]


# spec: ANYM-444
def test_a_classifier_flooding_stderr_still_names_its_class(tmp_path, monkeypatch):
    classifier = b"import sys\nsys.stderr.write('x' * (1 << 20))\ndef rollout_class(text):\n    return 'expand'\n"
    reason, _, started = classified(tmp_path, monkeypatch, ONE_EXPAND, classifier)
    assert reason.endswith("; added migrations: 041_synthetic_expand.sql expand") and len(started) == 1


def test_deploy_continues_when_the_changed_deploy_tools_are_the_installed_ones(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch, changes={NEWER: ["scripts/production.py", "scripts/seamless_release.py"]})
    host.blobs = {f"{NEWEST}:scripts/production.py": p.blob(p.__file__),
                  f"{NEWEST}:scripts/seamless_release.py": p.blob(host.s.__file__)}
    assert host.deploy()["status"] == "deployed"


@pytest.mark.parametrize("change", ["settings", "compose", "http"])
def test_deploy_pauses_when_the_configuration_changed_since_the_current_release(tmp_path, monkeypatch, change):
    host = Deployer(tmp_path, monkeypatch)
    if change == "settings":
        host.settings["projects"][0]["services"]["indexer"] = "indexer"
    elif change == "compose":
        host.document["services"]["api"]["environment"] = {"SYNTHETIC": "changed by hand"}
    else:
        host.http["http_release"]["drain_timeout"] = 1
    outcome = host.deploy()
    assert outcome["status"] == "halted" and "changed since" in outcome["reason"] and host.steps == []


MODULE_MANIFEST = (ROOT / "deploy/modules/recordings.json").read_text()


def enroll_required(sha):
    return f"enroll_required: recordings: production.py enroll --module recordings --sha {sha} --plan"


# spec: ANYM-745
def test_a_module_manifest_in_the_range_pauses_with_the_enroll_command_and_enrolls_nothing(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch, changes={NEWEST: ["deploy/modules/recordings.json"]})
    host.contents[f"{NEWEST}:deploy/modules/recordings.json"] = MODULE_MANIFEST
    outcome = host.deploy(auto=True)
    assert outcome["status"] == "halted" and outcome["reason"] == enroll_required(NEWEST)
    assert host.steps == [] and host.paused() == outcome["reason"]


# spec: ANYM-745
def test_an_enrollment_after_the_selected_release_does_not_block_it(tmp_path, monkeypatch):
    third = "3" * 40  # main's tip: its CI has not attested it yet, so NEWEST is the release this run deploys
    host = Deployer(tmp_path, monkeypatch, history=(NEWER, NEWEST, third), green=(NEWER, NEWEST),
                    changes={third: ["deploy/modules/recordings.json"]})
    host.contents[f"{third}:deploy/modules/recordings.json"] = MODULE_MANIFEST
    assert host.deploy(auto=True)["status"] == "deployed"


# spec: ANYM-745
def test_an_unchanged_malformed_manifest_does_not_halt_an_ordinary_release(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    host.contents[f"{NEWEST}:deploy/modules/broken.json"] = '{"name": "broken", "project": {"services": {"x": []}}}'
    assert host.deploy(auto=True)["status"] == "deployed" and host.paused() is None


# spec: ANYM-745
def test_a_manifest_for_a_module_the_host_already_runs_is_an_ordinary_release(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch, changes={NEWEST: ["deploy/modules/recordings.json"]})
    host.contents[f"{NEWEST}:deploy/modules/recordings.json"] = MODULE_MANIFEST
    host.settings["projects"].append({"name": "deedkit-recordings", "directory": str(tmp_path), "files": [],
                                      "services": {"recordings-api": "recordings"}})
    # The configuration differs from the recorded one by that enrollment only, so the pause names the command.
    assert host.deploy(auto=True)["reason"] == enroll_required(NEWEST)


# spec: ANYM-745
def test_a_configuration_that_differs_only_by_an_enrollment_pauses_with_the_enroll_command(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    host.contents[f"{NEWEST}:deploy/modules/recordings.json"] = MODULE_MANIFEST
    host.settings["projects"][0]["files"].append(str(tmp_path / "compose.gateway-recordings.yaml"))
    host.settings["projects"].append({"name": "deedkit-recordings", "directory": str(tmp_path),
                                      "files": [str(tmp_path / "compose.recordings.yaml")],
                                      "services": {"recordings-api": "recordings"}})
    outcome = host.deploy(auto=True)
    assert outcome["status"] == "halted" and outcome["reason"] == enroll_required(NEWEST) and host.steps == []


# spec: ANYM-765
def test_a_running_module_with_a_broken_changed_manifest_pauses_with_its_real_cause(tmp_path, monkeypatch):
    path = "deploy/modules/recordings.json"
    host = Deployer(tmp_path, monkeypatch, changes={NEWEST: [path]})
    host.contents[f"{BASE}:{path}"], host.contents[f"{NEWEST}:{path}"] = MODULE_MANIFEST, "{not json"
    host.settings["projects"].append({"name": "deedkit-recordings", "directory": str(tmp_path), "files": [],
                                      "services": {"recordings-api": "recordings"}})
    host.current(BASE)  # the module runs in the current release: nothing drifts
    outcome = host.deploy(auto=True)
    assert outcome["status"] == "halted" and outcome["manifest_code"] == "invalid" and outcome["sha"] == NEWEST
    assert outcome["reason"] == "manifest_invalid: recordings: fix deploy/modules/recordings.json; enroll is not needed"
    assert host.steps == [] and host.paused() == outcome["reason"]


# spec: ANYM-765
def test_a_broken_manifest_of_a_module_that_was_not_running_keeps_the_enroll_pause(tmp_path, monkeypatch):
    path = "deploy/modules/recordings.json"
    host = Deployer(tmp_path, monkeypatch, changes={NEWEST: [path]})
    host.contents[f"{BASE}:{path}"], host.contents[f"{NEWEST}:{path}"] = MODULE_MANIFEST, "{not json"
    outcome = host.deploy(auto=True)
    assert outcome["reason"] == enroll_required(NEWEST) and "manifest_code" not in outcome and host.steps == []


# spec: ANYM-745
def test_a_drift_beyond_an_enrollment_keeps_the_apply_by_hand_pause(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    host.contents[f"{NEWEST}:deploy/modules/recordings.json"] = MODULE_MANIFEST
    host.settings["projects"].append({"name": "deedkit-recordings", "directory": str(tmp_path), "files": [],
                                      "services": {"recordings-api": "recordings"}})
    host.settings["projects"][0]["services"]["indexer"] = "indexer"  # something else changed too
    outcome = host.deploy(auto=True)
    assert outcome["status"] == "halted" and outcome["reason"].endswith("; apply the next release by hand")
    assert "enroll_required" not in outcome["reason"]


# spec: ANYM-745
def test_production_md_names_every_enrollment_refusal_code_and_the_pause_text():
    docs = " ".join((ROOT / "docs/operations/production.md").read_text().split())
    assert not [code for code in p.ENROLL_CODES if f"`{code}`" not in docs]
    assert "enroll_required: <module>: production.py enroll --module <module> --sha <sha> --plan" in docs
    assert "plan_digest" in docs and "deploy/modules/<module>.json" in docs


# spec: ANYM-765
def test_production_md_states_the_manifest_invalid_pause_and_the_earlier_network_refusal():
    docs = " ".join((ROOT / "docs/operations/production.md").read_text().split())
    assert "manifest_invalid: <module>: fix deploy/modules/<module>.json; enroll is not needed" in docs
    assert "is refused as `network_unsafe` before the pause, the receipt or any file is written" in docs


HALT_CALLS = 43  # every halt(...) of the tool files; a new call raises this number and adds its code to p.HALTS


def halt_call_problems(source, table):
    """The number of `halt(...)` calls in `source` (one text, or the texts of every tool file) and, by line or by
    code, what breaks the parity with `table`."""
    calls, used, problems = 0, set(), []
    for node in (node for text in ([source] if isinstance(source, str) else source) for node in ast.walk(ast.parse(text))):
        if not isinstance(node, ast.Call):
            continue
        if (node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)) != "halt":
            continue
        calls += 1
        code = next((keyword.value for keyword in node.keywords if keyword.arg == "code"), None)
        if not (isinstance(code, ast.Constant) and isinstance(code.value, str)):
            problems.append(f"line {node.lineno}: halt has no literal code=")
        elif code.value not in table:
            problems.append(f"line {node.lineno}: code {code.value} is not in the table")
        else:
            used.add(code.value)
    return calls, problems + [f"code {code} has no call site" for code in sorted(set(table) - used)]


# spec: ANYM-766
def test_every_halt_call_has_a_literal_code_of_the_table_and_the_table_no_unused_code():
    # Every tool file (TOOL_FILES): a halt moved out of production.py still counts (W5-1b).
    calls, problems = halt_call_problems([(ROOT / path).read_text() for path in p.TOOL_FILES], p.HALTS)
    assert problems == [] and calls == HALT_CALLS


# spec: ANYM-766
def test_the_halt_parity_walk_names_each_kind_of_break():
    source = ('halt("a")\nhalt(reason, code=variable)\nhalt(reason, **fields)\nfinish(**halt(helper(), code="other"))\n'
              'self.halt("b", code="known")\n')
    assert halt_call_problems(source, {"known": 0, "unused": 0}) == (5, [
        "line 1: halt has no literal code=", "line 2: halt has no literal code=", "line 3: halt has no literal code=",
        "line 4: code other is not in the table", "code unused has no call site"])


# spec: ANYM-766
def test_each_halt_names_an_actor_and_one_command_or_an_existing_document_anchor():
    docs = (ROOT / "docs/operations/production.md").read_text()
    anchors = {re.sub(r"[^a-z0-9 -]", "", line.lstrip("#").strip().lower()).replace(" ", "-")
               for line in docs.splitlines() if re.match(r"#+ ", line)}
    # No halt waits for section 2's owner items: production changes are agents' under AGENTS.md section 2 (D25).
    assert {code for code, (actor, _) in p.HALTS.items() if actor != "agent"} == set()
    assert p.BackupRunning.code not in p.HALTS  # print_result reads that code as its exit 75
    for code, (_, step) in p.HALTS.items():
        document, _, anchor = step.partition("#")
        assert (document == "docs/operations/production.md" and anchor in anchors) or (
            not anchor and step.startswith(("production.py ", "seamless_release.py "))), code


# spec: ANYM-766
def test_a_halt_that_may_have_no_http_attempt_or_sha_routes_to_the_document_not_the_http_status_command():
    # Sol r1: an unfinished plan A apply, a preparation that wrote no attempt receipt, and a reason that names no SHA.
    for code in ("attempt_unresolved", "http_release_unsettled", "plan_b_preparation_unfinished"):
        assert p.HALTS[code][1] == "docs/operations/production.md#continuous-deployment"


# spec: ANYM-766
def test_the_halts_table_of_production_md_is_the_table_of_the_tool():
    docs = (ROOT / "docs/operations/production.md").read_text()
    section = docs.split("\n### Halts\n", 1)[1].split("\n### ", 1)[0]
    rows = "".join(f"| `{code}` | {actor} | `{step}` |\n" for code, (actor, step) in sorted(p.HALTS.items()))
    assert "| Code | Actor | Next |\n|---|---|---|\n" + rows in section
    assert docs.count("(#halts)") >= 2


# spec: ANYM-745
def test_the_review_signal_grep_and_the_deploy_rules_cover_module_manifests():
    protocol = (ROOT / "docs/operations/review-protocol.md").read_text()
    pattern = re.search(r"grep -E -i '([^']+)'", protocol)[1]
    assert re.search(pattern, "deploy/modules/recordings.json", re.IGNORECASE)
    rules = (ROOT / ".claude/rules/deploy.md").read_text()
    assert '"deploy/**"' in rules


# spec: ANYM-711
def test_recordings_enrollment_and_undo_are_hand_releases_through_both_plans(tmp_path, monkeypatch):
    third = "3" * 40
    host = Deployer(tmp_path, monkeypatch, history=(NEWER, NEWEST, third), green=(NEWER, NEWEST, third))
    unrelated = copy.deepcopy(host.settings["projects"])
    overlay = str(tmp_path / "compose.gateway-recordings.yaml")
    project = host.settings["projects"][0]  # the same dict serves plan A and plan B
    project["files"].append(overlay)
    host.settings["projects"].append({"name": "deedkit-recordings", "directory": str(tmp_path),
                                      "files": [str(tmp_path / "compose.recordings.yaml")],
                                      "services": {"recordings-api": "recordings"}})
    # Both configurations changed: autodeploy halts before any step and leaves the pause file.
    outcome = host.deploy()
    assert outcome["status"] == "halted" and "changed since" in outcome["reason"] and host.steps == []
    assert host.paused() == outcome["reason"]
    # A dry run under the pause file answers `paused` before it looks at drift.
    assert host.deploy(dry_run=True)["status"] == "paused"
    # The hand release: plan A, then plan B (promote re-records the HTTP digest), then the pause goes.
    for step, configuration in ((p.prepare, host.settings), (p.apply, host.settings),
                                (host.s.prepare, host.http), (host.s.promote, host.http)):
        step(configuration, NEWER)
    assert [name for name, _ in host.steps] == ["prepare", "apply", "seamless prepare", "seamless promote"]
    p.PAUSE.unlink()
    assert p.read(host.http_state / "http-current.json")["config_digest"] == p.digest(host.http)
    plan = host.deploy(dry_run=True)
    assert plan["status"] != "halted" and "changed since" not in json.dumps(plan)
    # Undo: the configurations go back; autodeploy halts again and does not undo anything itself.
    project["files"].remove(overlay)
    host.settings["projects"] = [project]
    host.steps.clear()
    halted = host.deploy()
    assert halted["status"] == "halted" and "changed since" in halted["reason"] and host.steps == []
    # The undo is a release at a SHA newer than any prepared with recordings, through both plans.
    for step, configuration in ((p.prepare, host.settings), (p.apply, host.settings),
                                (host.s.prepare, host.http), (host.s.promote, host.http)):
        step(configuration, third)
    p.PAUSE.unlink()
    assert host.deploy() == {"status": "current", "sha": third}
    assert host.settings["projects"] == unrelated


# spec: ANYM-711
def test_enrollment_and_the_fresh_sha_undo_pass_the_real_preparation_guards(tmp_path, monkeypatch):
    # The real prepare(), receipt files and drift check; only the Docker, git and registry calls are the fixture's.
    settings, _, _ = fixture(tmp_path, monkeypatch)
    second, third = "2" * 40, "3" * 40
    fixture_run = p.run
    stores, wanted = {}, [SHA]  # one synthetic image store per commit: identities, revision labels and recipes per target
    def routed(argv, **kwargs):
        if "FETCH_HEAD" in argv:
            return wanted[0]
        store = stores.setdefault(wanted[0], ImageStore())
        if argv[:2] in (["docker", "pull"], ["docker", "tag"], ["docker", "run"]) or argv[:3] in (
                ["docker", "image", "ls"], ["docker", "image", "inspect"]):
            try:
                return store.run(argv, **kwargs)
            except (KeyError, StopIteration):
                pass  # an image the store never held (the running container's): the fixture answers
        return fixture_run(argv, **kwargs)
    monkeypatch.setattr(p, "run", routed)
    monkeypatch.setattr(p, "published", lambda target, sha: stores.setdefault(sha, ImageStore()).published(target, sha))
    plain_digest = p.settings_digest(settings)
    state = Path(settings["state_directory"])
    p.prepare(settings, SHA)
    assert p.read(state / SHA / "prepared.json")["config_digest"] == plain_digest
    (tmp_path / "recordings.json").write_text(json.dumps({"name": "deedkit-recordings", "services": {
        "recordings-api": {"image": "old", "build": {"context": "/unused"}}}}))
    enrolled = copy.deepcopy(settings)
    enrolled["projects"][0]["files"].append(str(tmp_path / "compose.gateway-recordings.yaml"))
    enrolled["projects"].append({"name": "deedkit-recordings", "directory": str(tmp_path),
                                 "files": [str(tmp_path / "recordings.json")], "services": {"recordings-api": "recordings"}})
    (tmp_path / "compose.gateway-recordings.yaml").write_text("{}")
    assert p.settings_digest(enrolled) != plain_digest
    current = {"sha": SHA, "rollback": {"projects": []}}
    assert "production configuration changed since" in p.configuration_drift(enrolled, {}, current, {})
    # The enrollment release prepares both projects at a SHA not prepared before.
    wanted[:] = [second]
    receipt = p.prepare(enrolled, second)
    assert [item["name"] for item in receipt["projects"]] == ["synthetic", "deedkit-recordings"]
    assert p.read(state / second / "prepared.json")["config_digest"] == p.settings_digest(enrolled)
    # The undo cannot reuse the enrolled SHA; a newer one prepares the plain configuration again.
    with pytest.raises(p.Refused, match="already exists"):
        p.prepare(settings, second)
    wanted[:] = [third]
    undo = p.prepare(settings, third)
    assert [item["name"] for item in undo["projects"]] == ["synthetic"]
    assert p.read(state / third / "prepared.json")["config_digest"] == plain_digest


# spec: ANYM-711
def test_a_sha_prepared_with_recordings_cannot_be_prepared_again(tmp_path, monkeypatch):
    settings, _, _ = fixture(tmp_path, monkeypatch)
    p.prepare(settings, SHA)
    with pytest.raises(p.Refused, match="already exists"):
        p.prepare(settings, SHA)


def test_a_failed_health_wait_rolls_back_once_and_pauses(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    def unhealthy(sha):
        p.write(host.state / sha / "result.json", {"sha": sha, "status": "unhealthy"})
        raise p.Refused("a replacement failed its health wait")
    host.fail_apply = unhealthy
    outcome = host.deploy()
    assert host.steps == [("prepare", NEWEST), ("apply", NEWEST), ("rollback", NEWEST)]
    assert outcome["status"] == "rolled-back" and f"rolled back to {BASE}" in outcome["reason"]
    assert p.read(host.state / "deploy.json")["status"] == "rolled-back" and host.paused() == outcome["reason"]
    # After a human removes the pause, the failed commit is never retried and nothing older follows it.
    p.PAUSE.unlink()
    host.steps.clear()
    assert host.deploy()["status"] == "waiting" and host.steps == []


# spec: ANYM-758
def test_a_replacement_looping_after_the_real_apply_health_wait_rolls_back_once_and_pauses(tmp_path, monkeypatch):
    # `deploy` on the Deployer's mirror and GitHub fakes, with the real prepare and apply on Host's synthetic Docker.
    real = {"prepare": p.prepare, "apply": p.apply}
    (tmp_path / "docker").mkdir()
    docker = Host(tmp_path / "docker", monkeypatch)
    host = Deployer(tmp_path, monkeypatch)
    docker.state, docker.main = host.state, NEWEST
    docker.settings["state_directory"] = str(host.state)
    host.settings, host.document = docker.settings, json.loads(docker.original.read_text())
    host.current(BASE)
    inside, inspects = [], []
    def run(argv, **kwargs):
        if argv[:2] == ["docker", "inspect"] and docker.container["Image"] == docker.image(NEWEST):
            # Running when `up --wait` returns and at apply's first inspect, restarting after the settle.
            state, restarts = ((RUNNING, 0), (LOOPING, 1))[min(len(inspects), 1)]
            docker.container.update({"State": dict(state), "RestartCount": restarts})
            inspects.append(argv)
        return docker.run(argv, **kwargs) if inside or argv[0] == "docker" else host.run(argv, **kwargs)
    monkeypatch.setattr(p, "run", run)
    def step(name):
        def call(settings, sha, *rest, **options):
            host.steps.append((name, sha))
            inside.append(name)
            try:
                return real[name](settings, sha, *rest, **options)
            finally:
                inside.pop()
        return call
    for name in real:
        monkeypatch.setattr(p, name, step(name))
    clock = Clock()
    monkeypatch.setattr(p, "time", clock)
    monkeypatch.setattr(p, "APPLY_SETTLE_SECONDS", 10)
    outcome = host.deploy()
    assert host.steps == [("prepare", NEWEST), ("apply", NEWEST), ("rollback", NEWEST)]
    assert p.read(host.state / NEWEST / "result.json")["status"] == "unhealthy" and 10 in clock.sleeps
    assert outcome["status"] == "rolled-back" and f"rolled back to {BASE}" in outcome["reason"]
    assert p.read(host.state / "deploy.json")["status"] == "rolled-back" and host.paused() == outcome["reason"]
    p.PAUSE.unlink()
    host.steps.clear()
    assert host.deploy()["status"] == "waiting" and host.steps == []


def test_a_failed_rollback_or_an_unknown_apply_state_pauses_without_any_further_step(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    def unhealthy(sha):
        p.write(host.state / sha / "result.json", {"sha": sha, "status": "unhealthy"})
        raise p.Refused("a replacement failed its health wait")
    host.fail_apply = unhealthy
    host.fail_rollback = lambda sha: (_ for _ in ()).throw(p.Refused("restored service is missing"))
    outcome = host.deploy()
    assert outcome["status"] == "halted" and "rollback failed: restored service is missing" in outcome["reason"]
    (tmp_path / "unknown").mkdir()
    host = Deployer(tmp_path / "unknown", monkeypatch)
    host.fail_apply = lambda sha: (_ for _ in ()).throw(p.Refused("replacement did not start the prepared image"))
    outcome = host.deploy()
    assert host.steps == [("prepare", NEWEST), ("apply", NEWEST)]
    assert outcome["status"] == "halted" and "unknown state" in outcome["reason"]
    # The unfinished attempt keeps every later run paused, even after the pause file is removed.
    p.PAUSE.unlink()
    assert host.deploy()["status"] == "halted" and "unfinished" in host.paused()


def test_a_refusal_before_any_change_pauses_and_a_network_or_quota_error_is_retried(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    host.fail_prepare = lambda sha: (_ for _ in ()).throw(p.Refused("latest main CI run is not a completed success"))
    outcome = host.deploy()
    assert outcome["status"] == "halted" and "refused before any change" in outcome["reason"]
    for error in (TimeoutError(), p.RateLimited("GitHub rate limit exhausted, resets at 12:00:00Z"),
                  p.Unreachable("main could not be read: git failed (exit 128)")):
        (tmp_path / type(error).__name__).mkdir()
        host = Deployer(tmp_path / type(error).__name__, monkeypatch)
        def early(sha, host=host, error=error):
            (host.state / sha / "attempted.json").unlink()  # it failed in its first verification
            raise error
        host.fail_apply = early
        outcome = host.deploy()
        assert outcome["status"] == "error" and "the next run retries" in outcome["reason"] and host.paused() is None
        host.fail_apply = None
        host.steps.clear()
        # The retry applies the prepared release without preparing it again.
        assert host.deploy()["status"] == "deployed" and host.steps[0] == ("apply", NEWEST)

BACKUP_IMAGE_ID = "sha256:" + "c" * 64
# A synthetic runner: its read-only --check exits 3 on a config that names an unfinished journal, else 0.
RUNNER_CHECK = (b"import json, sys\nconfig = json.load(open(sys.argv[sys.argv.index('--config') + 1]))\n"
                b"sys.exit(3 if config.get('unfinished') else 0)\n")


def runner_host(tmp_path, monkeypatch, *, unit="inactive", revision=NEWEST, pull=None, calls=None):
    """A Deployer host whose backup runner is older than NEWEST's, with messaging's and the calendar's runner.json
    (owner mode 0640) pinning OLD_IMAGE and no recordings profile; the units report `unit`, the candidate's backup
    image carries `revision`, its pull raises `pull` and the call check raises `calls`."""
    host = Deployer(tmp_path, monkeypatch)
    host.events = []
    (tmp_path / "ops").mkdir()
    runner = tmp_path / "ops" / "backup_runner.py"
    runner.write_bytes(b"# synthetic older runner\n" + RUNNER_CHECK)
    runner.chmod(0o600)
    configs = []
    for name in ("backup-config", "calendar-backup"):
        (tmp_path / name).mkdir()
        configs.append(tmp_path / name / "runner.json")
        configs[-1].write_text('{\n  "image": "%s",\n  "directory": "/synthetic/%s"\n}\n' % (OLD_IMAGE, name))
        configs[-1].chmod(0o640)
    monkeypatch.setattr(p, "BACKUP_RUNNER", runner)
    monkeypatch.setattr(p, "RUNNER_CONFIGS", (*configs, tmp_path / "recordings-backup" / "runner.json"))
    monkeypatch.setattr(p, "RUNNER_PYTHON", sys.executable)
    new = b"# synthetic candidate runner\n" + RUNNER_CHECK
    host.contents[f"{NEWEST}:{p.BACKUP_RUNNER_SOURCE}"] = new
    host.blobs[f"{NEWEST}:{p.BACKUP_RUNNER_SOURCE}"] = p.git_blob(new)
    def command(argv, **kwargs):
        if argv[0] == "systemctl":
            host.events.append(("unit", argv[-1]))
            return unit + "\n"
        if argv[:3] == ["docker", "image", "inspect"]:
            return f"{argv[-1]} {revision}\n"
        if argv[0] == sys.executable:
            host.events.append(("check", Path(argv[2]).name, Path(argv[-2]).name))
            return Deployer.real_run(argv, **kwargs)
        return host.run(argv, **kwargs)
    monkeypatch.setattr(p, "run", command)
    def backup_image(source, sha, target, **kwargs):
        host.events.append(("pull", sha, target))
        if pull:
            raise pull
        return BACKUP_IMAGE_ID
    monkeypatch.setattr(p, "build_image", backup_image)
    def call_check(settings, deadline):
        host.events.append(("calls",))
        if calls:
            raise calls
    monkeypatch.setattr(p, "wait_for_calls", call_check)
    return host, runner, configs, new


# spec: ANYM-488
def test_an_automatic_deploy_installs_the_candidates_backup_runner_with_its_image_pin(tmp_path, monkeypatch):
    host, runner, configs, new = runner_host(tmp_path, monkeypatch)
    old, pins = runner.read_bytes(), [config.read_bytes() for config in configs]
    host.fail_apply = lambda sha: p.check_backup_runner(host.state / sha / "source", sha)  # apply's own check passes
    outcome = host.deploy(auto=True)
    assert outcome["status"] == "deployed" and host.paused() is None and ("apply", NEWEST) in host.steps
    assert runner.read_bytes() == new and oct(runner.stat().st_mode & 0o777) == "0o600"
    assert runner.with_name(f"backup_runner.py.before-{NEWEST}").read_bytes() == old
    for config, pin in zip(configs, pins, strict=True):
        assert json.loads(config.read_text()) == {**json.loads(pin), "image": BACKUP_IMAGE_ID}
        assert config.with_name(f"runner.json.before-{NEWEST}").read_bytes() == pin
        assert oct(config.stat().st_mode & 0o777) == "0o640"
        assert sorted(path.name for path in config.parent.iterdir()) == ["runner.json", f"runner.json.before-{NEWEST}"]
    # Every unit is idle, the new runner checks each runner.json as it will be, before the first rename.
    assert [event[1] for event in host.events if event[0] == "unit"] == list(p.RUNNER_UNITS)
    assert [event for event in host.events if event[0] == "check"] == [
        ("check", f".backup_runner.py.{NEWEST}.tmp", f".runner.json.{NEWEST}.tmp")] * 2
    receipt = p.read(host.state / NEWEST / "backup-runner.json")
    assert receipt["status"] == "installed" and receipt["image"] == BACKUP_IMAGE_ID
    assert [item["path"] for item in receipt["files"]] == [str(runner), *map(str, configs)]
    # A run that stopped after the runner's rename and before its receipt: the next run settles the receipt only.
    p.write_record(host.state / NEWEST / "backup-runner.json", {**receipt, "status": "installing"})
    host.events.clear()
    p.install_backup_runner(host.settings, NEWEST, host.state / NEWEST)
    assert host.events == [] and p.read(host.state / NEWEST / "backup-runner.json")["status"] == "installed"


# spec: ANYM-782
def test_a_backup_runner_install_pins_the_calls_config_and_waits_for_the_calls_units():
    assert Path("/opt/deedkit/calls-backup/runner.json") in p.RUNNER_CONFIGS
    assert {"deedkit-calls-backup.service", "deedkit-calls-backup-catchup.service"} <= set(p.RUNNER_UNITS)


def with_calls_config(tmp_path, monkeypatch, **extra):
    config = tmp_path / "calls-backup" / "runner.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"image": OLD_IMAGE, "directory": "/synthetic/calls", **extra}, indent=2) + "\n")
    config.chmod(0o600)
    monkeypatch.setattr(p, "RUNNER_CONFIGS", (*p.RUNNER_CONFIGS, config))
    return config


# spec: ANYM-782
def test_an_automatic_deploy_moves_the_calls_pin_keeping_its_mode_and_a_copy(tmp_path, monkeypatch):
    host, _, _, new = runner_host(tmp_path, monkeypatch)
    config = with_calls_config(tmp_path, monkeypatch)
    pin = config.read_bytes()
    host.fail_apply = lambda sha: p.check_backup_runner(host.state / sha / "source", sha)
    assert host.deploy(auto=True)["status"] == "deployed"
    assert json.loads(config.read_text())["image"] == BACKUP_IMAGE_ID and oct(config.stat().st_mode & 0o777) == "0o600"
    assert config.with_name(f"runner.json.before-{NEWEST}").read_bytes() == pin


# spec: ANYM-782
def test_a_calls_config_the_new_runner_refuses_changes_nothing(tmp_path, monkeypatch):
    host, runner, configs, _ = runner_host(tmp_path, monkeypatch)
    config = with_calls_config(tmp_path, monkeypatch, unfinished=True)
    before = [path.read_bytes() for path in (runner, *configs, config)]
    assert host.deploy(auto=True)["status"] != "deployed"
    assert [path.read_bytes() for path in (runner, *configs, config)] == before


# spec: ANYM-488
@pytest.mark.parametrize("case", ["unit-active", "unfinished-journal", "other-image", "pull-failed", "pull-timeout",
                                  "call-active", "calls-unknown"])
def test_a_backup_runner_that_cannot_be_installed_safely_changes_nothing(tmp_path, monkeypatch, case):
    # A failed `docker pull` reaches build_image as run()'s plain Refused; a hung one as subprocess's TimeoutExpired.
    pulls = {"pull-failed": p.Refused("docker failed (exit 1); production was not accepted"),
             "pull-timeout": subprocess.TimeoutExpired(["docker", "pull"], 1800)}
    calls = {"call-active": p.CallActive("1 call is active"), "calls-unknown": p.CallsUnknown("calls_probe_failed")}
    host, runner, configs, _ = runner_host(
        tmp_path, monkeypatch, unit="activating" if case == "unit-active" else "inactive",
        revision=NEWER if case == "other-image" else NEWEST, pull=pulls.get(case), calls=calls.get(case))
    if case == "unfinished-journal":
        configs[1].write_text('{"image": "%s", "directory": "/synthetic/calendar", "unfinished": true}\n' % OLD_IMAGE)
    files = {path: path.read_bytes() for path in (runner, *configs)}
    outcome = host.deploy(auto=True)
    assert {path: path.read_bytes() for path in files} == files
    assert all(sorted(child.name for child in path.parent.iterdir()) == [path.name] for path in files)
    assert ("apply", NEWEST) not in host.steps and not (host.state / NEWEST / "backup-runner.json").exists()
    assert (host.state / NEWEST / "prepared.json").exists() and p.read(host.state / "current.json")["sha"] == BASE
    if case == "unit-active":
        assert outcome["status"] == "waiting" and outcome["reason"].startswith("personal-os-backup.service is activating")
    elif case in pulls:
        assert outcome["status"] == "waiting" and outcome["migration_waits"]["count"] == 1 and host.paused() is None
        assert outcome["reason"].startswith(f"the backup image of {NEWEST[:12]} could not be pulled")
    elif case in calls:
        # Checked last, after every other check: the staged files are gone again.
        assert outcome["status"] == "waiting" and outcome["call_waits"]["count"] == 1 and host.paused() is None
        assert [event[0] for event in host.events][-1] == "calls"
    else:
        assert outcome["status"] == "halted" and host.paused()
        assert ({"unfinished-journal": f"--check of {configs[1]}", "other-image": "is not its verified image"}[case]
                in outcome["reason"])



# spec: ANYM-488
@pytest.mark.parametrize("stop", [1, 2, 3])
def test_an_interrupted_backup_runner_install_never_runs_the_new_runner_on_an_old_pin_and_a_rerun_completes(
        tmp_path, monkeypatch, stop):
    # ANYM-627: a newer runner writes worker.json keys an older worker refuses, so every runner.json moves first.
    host, runner, configs, new = runner_host(tmp_path, monkeypatch)
    old, pins = runner.read_bytes(), [config.read_bytes() for config in configs]
    replace, renamed = os.replace, []
    def interrupted(source, target, **kwargs):
        if "dst_dir_fd" not in kwargs:  # a receipt or state file
            return replace(source, target, **kwargs)
        if len(renamed) + 1 == stop:
            raise OSError("synthetic crash")
        replace(source, target, **kwargs)
        renamed.append(target)
        assert runner.read_bytes() != new or all(
            json.loads(config.read_text())["image"] == BACKUP_IMAGE_ID for config in configs)
    monkeypatch.setattr(p.os, "replace", interrupted)
    outcome = host.deploy(auto=True)
    assert outcome["status"] == "halted" and "stopped after it began to rename" in outcome["reason"]
    assert renamed == ["runner.json"] * (stop - 1) and runner.read_bytes() == old
    assert p.read(host.state / NEWEST / "backup-runner.json")["status"] == "installing"
    # The operator reads the receipt and removes the pause; the .before copies are the old bytes, so the rerun installs.
    p.PAUSE.unlink()
    monkeypatch.setattr(p.os, "replace", replace)
    monkeypatch.setattr(p, "DEPLOY_INTERVAL", 0)
    assert host.deploy(auto=True)["status"] == "deployed"
    assert runner.read_bytes() == new and runner.with_name(f"backup_runner.py.before-{NEWEST}").read_bytes() == old
    for config, pin in zip(configs, pins, strict=True):
        assert json.loads(config.read_text())["image"] == BACKUP_IMAGE_ID
        assert config.with_name(f"runner.json.before-{NEWEST}").read_bytes() == pin
        assert sorted(path.name for path in config.parent.iterdir()) == ["runner.json", f"runner.json.before-{NEWEST}"]
    assert p.read(host.state / NEWEST / "backup-runner.json")["status"] == "installed"


MODULE = b"# synthetic candidate profiles module\n"
# A synthetic candidate runner that, like the W5-9 runner, loads its profiles module from its own directory by its own
# name (`backup_runner` -> `backup_profiles`): its --check exits 4 unless that file holds the candidate's module.
SIBLING_CHECK = (b"import pathlib, sys\nhere = pathlib.Path(sys.argv[0])\n"
                 b"module = here.with_name(here.name.replace('backup_runner', 'backup_profiles', 1))\n"
                 b"sys.exit(4) if not module.exists() or module.read_bytes() != %r else None\n" % MODULE)


def with_profiles(host, monkeypatch, runner, installed=None):
    """The candidate NEWEST ships `backup_profiles.py` (MODULE) and a runner that loads it; the host has `installed`."""
    new = b"# synthetic candidate runner\n" + SIBLING_CHECK + RUNNER_CHECK
    host.contents[f"{NEWEST}:{p.BACKUP_RUNNER_SOURCE}"] = new
    host.blobs[f"{NEWEST}:{p.BACKUP_RUNNER_SOURCE}"] = p.git_blob(new)
    host.contents[f"{NEWEST}:{p.BACKUP_PROFILES_SOURCE}"] = MODULE
    host.blobs[f"{NEWEST}:{p.BACKUP_PROFILES_SOURCE}"] = p.git_blob(MODULE)
    module = runner.with_name("backup_profiles.py")
    if installed is not None:
        module.write_bytes(installed)
        module.chmod(0o600)
    replace, renamed = os.replace, []
    def recorded(source, target, **kwargs):
        renamed.append(target) if "dst_dir_fd" in kwargs else None
        return replace(source, target, **kwargs)
    monkeypatch.setattr(p.os, "replace", recorded)
    return new, module, renamed


# spec: ANYM-488
@pytest.mark.parametrize("installed", [None, b"# synthetic older profiles module\n"])
def test_a_release_with_a_profiles_module_installs_it_beside_the_runner_before_any_rename_of_the_runner(
        tmp_path, monkeypatch, installed):
    # W5-9: the runner loads its recordings and calls profiles from `backup_profiles.py` in its own directory.
    host, runner, configs, _ = runner_host(tmp_path, monkeypatch)
    new, module, renamed = with_profiles(host, monkeypatch, runner, installed)
    host.fail_apply = lambda sha: p.check_backup_runner(host.state / sha / "source", sha)  # apply's own check passes
    assert host.deploy(auto=True)["status"] == "deployed" and ("apply", NEWEST) in host.steps
    assert runner.read_bytes() == new and module.read_bytes() == MODULE and oct(module.stat().st_mode & 0o777) == "0o600"
    # The staged runner's --check ran against the staged module (else it exits 4), and the module moved first.
    assert [event for event in host.events if event[0] == "check"] == [
        ("check", f".backup_runner.py.{NEWEST}.tmp", f".runner.json.{NEWEST}.tmp")] * 2
    assert renamed == ["backup_profiles.py", "runner.json", "runner.json", "backup_runner.py"]
    copy = module.with_name(f"backup_profiles.py.before-{NEWEST}")
    assert (copy.read_bytes() == installed) if installed else not copy.exists()
    assert not [path.name for path in runner.parent.iterdir() if path.name.endswith(".tmp")]
    files = p.read(host.state / NEWEST / "backup-runner.json")["files"]
    assert [item["path"] for item in files] == [str(runner), str(module), *map(str, configs)]
    assert files[1] == {"path": str(module), "before": p.git_blob(installed) if installed else None,
                        "after": p.git_blob(MODULE), "undo": str(copy) if installed else None}


# spec: ANYM-488
def test_a_profiles_module_the_new_runner_refuses_with_changes_nothing(tmp_path, monkeypatch):
    host, runner, configs, _ = runner_host(tmp_path, monkeypatch)
    _, module, renamed = with_profiles(host, monkeypatch, runner, b"# synthetic older profiles module\n")
    configs[1].write_text('{"image": "%s", "directory": "/synthetic/calendar", "unfinished": true}\n' % OLD_IMAGE)
    files = {path: path.read_bytes() for path in (runner, module, *configs)}
    assert host.deploy(auto=True)["status"] == "halted" and renamed == []
    assert {path: path.read_bytes() for path in files} == files
    assert sorted(path.name for path in runner.parent.iterdir()) == ["backup_profiles.py", "backup_runner.py"]


# spec: ANYM-488
@pytest.mark.parametrize("case", ["absent", "other"])
def test_apply_is_refused_while_the_profiles_module_beside_the_runner_is_not_the_releases(tmp_path, monkeypatch, case):
    host, runner, _, _ = runner_host(tmp_path, monkeypatch)
    _, module, _ = with_profiles(host, monkeypatch, runner)
    assert host.deploy(auto=True)["status"] == "deployed"
    p.check_backup_runner(host.state / NEWEST / "source", NEWEST)
    module.unlink() if case == "absent" else module.write_bytes(b"# a hand edit\n")
    with pytest.raises(p.Refused, match="profiles beside") as refused:
        p.check_backup_runner(host.state / NEWEST / "source", NEWEST)
    assert ("are blob absent" if case == "absent" else p.blob(module)[:12]) in str(refused.value)
    # A host without the runner checks nothing, as before.
    runner.unlink()
    assert p.check_backup_runner(host.state / NEWEST / "source", NEWEST) is None


def test_deploy_waits_for_the_api_quota_and_for_the_interval_between_automatic_deploys(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    # No API quota is read or waited for (ANYM-406): the harness fails any GitHub API request.
    assert not hasattr(p, "quota") and host.deploy()["status"] == "deployed"
    host.green.add("3" * 40)
    host.history.append("3" * 40)
    host.steps.clear()
    assert host.deploy(auto=True)["status"] == "waiting" and host.steps == []
    assert host.deploy()["sha"] == "3" * 40  # a manual run is not held back by the interval


# spec: ANYM-407
def test_an_automatic_deploy_starts_five_minutes_after_the_last_one_and_the_timer_runs_every_minute(tmp_path, monkeypatch):
    # ci-deploy-speed 11-09: the interval is about one deploy's duration, not the old 20 minutes of the API quota.
    assert p.DEPLOY_INTERVAL == 5 * 60
    timer = (ROOT / "deploy/deedkit-autodeploy.timer").read_text()
    assert "OnUnitInactiveSec=1min" in timer and "RandomizedDelaySec=10s" in timer
    host = Deployer(tmp_path, monkeypatch)
    assert host.deploy()["status"] == "deployed"
    host.green.add("3" * 40)
    host.history.append("3" * 40)
    receipt = p.read(host.state / "deploy.json")
    started = datetime.now(timezone.utc) - timedelta(minutes=4)
    p.write(host.state / "deploy.json", {**receipt, "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ")})
    outcome = host.deploy(auto=True)
    assert outcome == {"status": "waiting", "reason": "less than 5 minutes since the last deploy started"}
    started -= timedelta(minutes=2)
    p.write(host.state / "deploy.json", {**receipt, "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ")})
    assert host.deploy(auto=True)["sha"] == "3" * 40


# spec: ANYM-407
def test_an_automatic_run_retries_a_failed_read_only_after_the_retry_spacing_so_three_failures_still_take_minutes(
        tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch, green=())
    host.git_failures["ls-remote"] = "fatal: unable to access 'https://github.com/GoSync-Inc/deedkit.git/'\n"
    assert host.deploy(auto=True)["failures"] == 1
    # The next minute's run reads nothing and counts nothing until RETRY_SPACING after the counted failure.
    calls = len(host.git)
    outcome = host.deploy(auto=True)
    assert outcome["status"] == "waiting" and "a git or GHCR read failed; the next try is at" in outcome["reason"]
    assert len(host.git) == calls and p.read(host.state / "deploy.json")["fetch_failures"]["count"] == 1
    # A manual run is never held back; it reads and counts.
    assert host.deploy()["failures"] == 2
    # Past the spacing the automatic run reads again; the third counted failure pauses as before.
    receipt = p.read(host.state / "deploy.json")
    earlier = (datetime.now(timezone.utc) - timedelta(seconds=p.RETRY_SPACING + 1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    p.write(host.state / "deploy.json", {**receipt, "fetch_failures": {**receipt["fetch_failures"], "at": earlier}})
    outcome = host.deploy(auto=True)
    assert outcome["status"] == "halted" and outcome["reason"].startswith("git could not read main 3 times in a row")
    # The same spacing holds after an unreadable registry, and a record without a readable time never holds a run.
    moment = datetime(2026, 11, 9, 8, 0, tzinfo=timezone.utc)
    assert p.retry_after({"unreachable": {"count": 1, "at": "2026-11-09T07:57:00Z"}}, moment) == moment + timedelta(minutes=1)
    assert p.retry_after({"unreachable": {"count": 1, "at": "2026-11-09T07:55:00Z"}}, moment) is None
    assert p.retry_after({"fetch_failures": {"count": 1, "at": None}}, moment) is None and p.retry_after({}, moment) is None


def test_deploy_finishes_plan_b_when_plan_a_is_ahead_of_the_serving_http_release(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    host.current(NEWER)  # plan A applied NEWER; its HTTP release stopped before any change
    assert host.deploy()["status"] == "deployed"
    assert host.steps == [("seamless prepare", NEWER), ("seamless promote", NEWER)] and host.requests == []


def test_a_failed_http_release_pauses_and_leaves_plan_a_applied(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    host.fail_seamless_promote = lambda sha: (_ for _ in ()).throw(
        p.Refused("candidate HTTP readiness did not pass; serving release is unchanged"))
    outcome = host.deploy()
    assert outcome["status"] == "halted" and "HTTP release stopped" in outcome["reason"]
    assert p.read(host.state / "current.json")["sha"] == NEWEST and ("rollback", NEWEST) not in host.steps


def test_a_first_http_generation_that_keeps_its_legacy_instances_does_not_hold_deploy_back(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    # seamless_release.py leaves the first managed generation pending for good: legacy instances stay retained.
    p.write(host.http_state / "http-current.json", {"sha": BASE, "status": "serving-retirement-pending",
                                                    "previous": None, "config_digest": p.digest(host.http)})
    assert host.deploy()["status"] == "deployed" and ("seamless retire", BASE) not in host.steps


def test_a_failed_retirement_of_the_previous_http_generation_pauses(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    p.write(host.http_state / "http-current.json", {"sha": BASE, "status": "serving-retirement-pending",
                                                    "previous": {"generation": "g1"}, "config_digest": p.digest(host.http)})
    assert host.deploy() == {"status": "waiting", "reason": "the previous HTTP generation is still draining"}
    host.fail_seamless_retire = lambda sha: (_ for _ in ()).throw(p.Refused("router admission fence needs recovery inspection"))
    outcome = host.deploy()
    assert outcome["status"] == "halted" and "router admission fence" in outcome["reason"] and host.paused()
    assert host.steps == [("seamless retire", BASE)] * 2


def test_a_missing_or_malformed_receipt_before_any_step_pauses_with_its_reason(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    (host.state / BASE / "prepared.json").unlink()
    outcome = host.deploy()
    assert outcome["status"] == "halted" and outcome["reason"].startswith("deploy was refused before any change")
    assert host.paused() and host.steps == [] and not (host.state / "deploy.json").exists()


def test_an_unresolved_http_attempt_pauses_before_plan_a_changes_anything(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    (host.http_state / NEWER).mkdir()
    p.write(host.http_state / NEWER / "http-attempted.json", {"status": "recovery-required", "sha": NEWER})
    outcome = host.deploy()
    assert outcome["status"] == "halted" and "HTTP release attempt is unresolved: " + NEWER in outcome["reason"]
    assert host.steps == [] and host.requests == []
    # A startup that failed before traffic admission left the serving release unchanged: it no longer blocks.
    p.write(host.http_state / NEWER / "http-attempted.json", {"status": "startup-failed", "sha": NEWER})
    p.PAUSE.unlink()
    assert host.deploy()["sha"] == NEWEST


def test_a_program_or_state_error_before_any_change_pauses_and_only_network_errors_are_retried(tmp_path, monkeypatch):
    retried = (urllib.error.URLError("synthetic resolver failure"),
               urllib.error.HTTPError("https://api.github.com", 502, "Bad Gateway", {}, None))
    paused = (ValueError("malformed receipt"), KeyError("sha"),
              urllib.error.HTTPError("https://api.github.com", 404, "Not Found", {}, None))
    for index, error in enumerate(retried + paused):
        (tmp_path / str(index)).mkdir()
        host = Deployer(tmp_path / str(index), monkeypatch)
        def early(sha, host=host, error=error):
            (host.state / sha / "attempted.json").unlink()  # it failed before its attempt began
            raise error
        host.fail_apply = early
        outcome = host.deploy()
        if error in retried:
            assert outcome["status"] == "error" and host.paused() is None, error
        else:
            assert outcome["status"] == "halted" and "refused before any change" in outcome["reason"], error
            assert host.paused() is not None


@pytest.mark.parametrize("receipt", ["{", '{"started_at": "yesterday"}', "[]"])
def test_a_malformed_previous_deploy_receipt_pauses_with_its_reason(tmp_path, monkeypatch, capsys, receipt):
    host = Deployer(tmp_path, monkeypatch)
    (host.state / "deploy.json").write_text(receipt)
    outcome = host.deploy(auto=True)
    assert outcome["status"] == "halted" and "deploy.json is malformed" in outcome["reason"]
    assert host.paused() == outcome["reason"] and host.steps == [] and host.git == []
    # The command line prints the same outcome as its one JSON line and exits 1.
    p.PAUSE.unlink()
    monkeypatch.setattr(p.os, "geteuid", lambda: 0)
    monkeypatch.setattr(p, "lock", lambda state: contextlib.nullcontext())
    monkeypatch.setattr(host.s, "locks", lambda settings: contextlib.nullcontext())
    monkeypatch.setattr(p, "config", lambda name: host.settings if name == "plan-a.json" else host.http)
    monkeypatch.setattr(host.s, "validate_settings", lambda value: value)
    with pytest.raises(SystemExit) as stopped:
        p.main(["deploy", "--config", "plan-a.json", "--http-config", "plan-b.json", "--auto"])
    printed = json.loads(capsys.readouterr().out)
    assert stopped.value.code == 1 and printed["status"] == "halted" and "malformed" in printed["reason"]
    assert host.paused() == printed["reason"] and host.steps == []


def test_a_serving_http_attempt_that_http_current_does_not_name_pauses_before_plan_a(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    # promote marked NEWER serving and wrote its result, then stopped before it published http-current.json.
    for sha in (BASE, NEWER):
        (host.http_state / sha).mkdir()
        p.write(host.http_state / sha / "http-attempted.json", {"status": "serving", "sha": sha})
        p.write(host.http_state / sha / "http-result.json", {"sha": sha, "status": "serving-retirement-pending"})
    outcome = host.deploy()
    assert outcome["status"] == "halted" and outcome["reason"].startswith(
        "an HTTP release attempt serves, but http-current.json does not name it: " + NEWER + ";")
    assert host.steps == [] and host.requests == [] and host.paused() == outcome["reason"]
    # The same when plan A had already applied NEWER: plan B is not started either.
    (tmp_path / "ahead").mkdir()
    host = Deployer(tmp_path / "ahead", monkeypatch)
    host.current(NEWER)
    (host.http_state / NEWER).mkdir()
    p.write(host.http_state / NEWER / "http-attempted.json", {"status": "serving", "sha": NEWER})
    assert host.deploy()["status"] == "halted" and host.steps == []


def test_an_invalid_source_mirror_pauses_and_is_never_fetched_into(tmp_path, monkeypatch):
    for kind in ("empty", "not bare"):
        p.PAUSE.unlink(missing_ok=True)
        (tmp_path / kind).mkdir()
        host = Deployer(tmp_path / kind, monkeypatch)
        if kind == "empty":
            (host.state / "deploy-source.git").mkdir()  # git refuses it locally, before any network access
        else:
            subprocess.run(["git", "init", "--quiet", str(host.state / "deploy-source.git")], check=True)
        outcomes = [host.deploy() for _ in range(3)]
        assert [outcome["status"] for outcome in outcomes] == ["fetch_failed", "fetch_failed", "halted"]
        detail = "not a git repository" if kind == "empty" else "is not the bare source mirror"
        assert all(detail in outcome["reason"] for outcome in outcomes), outcomes
        assert host.paused() == outcomes[-1]["reason"] and "3 times in a row" in host.paused()
        assert host.steps == [] and not [argv for argv in host.git if "fetch" in argv]
    # A real bare mirror is fetched.
    p.PAUSE.unlink()
    (tmp_path / "bare").mkdir()
    host = Deployer(tmp_path / "bare", monkeypatch)
    subprocess.run(["git", "init", "--quiet", "--bare", str(host.state / "deploy-source.git")], check=True)
    assert host.deploy()["status"] == "deployed" and [argv for argv in host.git if "fetch" in argv]


@pytest.mark.parametrize("command, stderr", [
    ("ls-remote", "fatal: unable to access 'https://github.com/GoSync-Inc/deedkit.git/': Could not resolve host: "
                  "github.com\n"),
    ("clone", "error: RPC failed; HTTP 403 curl 22 The requested URL returned error: 403\nfatal: early EOF\n")],
    ids=["ls-remote", "clone"])
def test_a_failed_fetch_changes_nothing_and_only_the_third_in_a_row_pauses(tmp_path, monkeypatch, capsys, command,
                                                                         stderr):
    host = Deployer(tmp_path, monkeypatch, green=())
    tail = stderr.strip().splitlines()[-1]
    def failed(count):
        outcome = host.deploy()
        assert outcome == {"status": "fetch_failed", "reason": outcome["reason"], "failures": count}
        assert tail in outcome["reason"] and host.paused() is None and host.steps == []
        receipt = p.read(host.state / "deploy.json")["fetch_failures"]
        assert receipt["count"] == count and receipt["reason"] == outcome["reason"]
    host.git_failures[command] = stderr
    failed(1)
    failed(2)
    # A dry run reports the failure but records nothing.
    assert "3 times in a row" in host.deploy(dry_run=True)["would_pause"] and host.paused() is None
    assert p.read(host.state / "deploy.json")["fetch_failures"]["count"] == 2
    # A successful fetch resets the count.
    del host.git_failures[command]
    assert host.deploy()["status"] == "waiting" and "fetch_failures" not in p.read(host.state / "deploy.json")
    host.git_failures[command] = stderr
    failed(1)
    failed(2)
    outcome = host.deploy()
    assert outcome["status"] == "halted" and outcome["reason"].startswith("git could not read main 3 times in a row")
    assert tail in outcome["reason"] and host.paused() == outcome["reason"] and host.steps == []
    assert "fetch_failures" not in p.read(host.state / "deploy.json")
    # The command line exits 1 on a failed fetch, so the timer unit shows it.
    p.PAUSE.unlink()
    monkeypatch.setattr(p.os, "geteuid", lambda: 0)
    monkeypatch.setattr(p, "lock", lambda state: contextlib.nullcontext())
    monkeypatch.setattr(host.s, "locks", lambda settings: contextlib.nullcontext())
    monkeypatch.setattr(p, "config", lambda name: host.settings if name == "plan-a.json" else host.http)
    monkeypatch.setattr(host.s, "validate_settings", lambda value: value)
    with pytest.raises(SystemExit) as stopped:
        p.main(["deploy", "--config", "plan-a.json", "--http-config", "plan-b.json"])
    assert stopped.value.code == 1 and json.loads(capsys.readouterr().out)["status"] == "fetch_failed"


# spec: ANYM-407
def test_a_successful_fetch_ends_the_git_failure_count_for_every_later_write_of_the_run(tmp_path, monkeypatch):
    # Astra r1 on #791: the cleared count lived on in the run's receipt, so a GHCR count or its reset wrote it back.
    host = Deployer(tmp_path, monkeypatch)
    host.git_failures["ls-remote"] = "fatal: unable to access 'https://github.com/GoSync-Inc/deedkit.git/'\n"
    assert [host.deploy()["failures"] for _ in range(2)] == [1, 2]
    del host.git_failures["ls-remote"]
    host.unreached.add(NEWEST)
    assert host.deploy()["status"] == "error"
    receipt = p.read(host.state / "deploy.json")
    assert "fetch_failures" not in receipt and receipt["unreachable"]["count"] == 1
    host.unreached.clear()
    host.green = set()
    assert host.deploy()["status"] == "waiting"
    receipt = p.read(host.state / "deploy.json")
    assert "fetch_failures" not in receipt and "unreachable" not in receipt
    # A new git failure starts a new count: it never pauses as the third of the earlier two.
    host.git_failures["ls-remote"] = "fatal: unable to access 'https://github.com/GoSync-Inc/deedkit.git/'\n"
    assert host.deploy()["failures"] == 1 and host.paused() is None


def test_an_unchanged_main_after_failed_fetches_resets_the_count(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch, history=())
    host.git_failures["ls-remote"] = p.Refused("git failed (exit 128): fatal: synthetic; production was not accepted")
    assert host.deploy()["failures"] == 1 and host.deploy()["failures"] == 2
    del host.git_failures["ls-remote"]
    assert host.deploy() == {"status": "current", "sha": BASE}
    assert "fetch_failures" not in p.read(host.state / "deploy.json")


def test_a_bare_mirror_under_a_path_with_spaces_is_fetched(tmp_path, monkeypatch):
    (tmp_path / "with spaces").mkdir()
    host = Deployer(tmp_path / "with spaces", monkeypatch)
    subprocess.run(["git", "init", "--quiet", "--bare", str(host.state / "deploy-source.git")], check=True)
    assert host.deploy()["status"] == "deployed" and [argv for argv in host.git if "fetch" in argv]


def test_a_dry_run_reports_the_plan_or_the_pause_reason_and_changes_nothing(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    assert host.deploy(dry_run=True) == {"status": "plan", "sha": NEWEST, "base": BASE, "steps": ["plan A", "plan B"]}
    host.changes = {NEWEST: ["services/messaging/messaging_service/migrations/036_synthetic.sql"]}
    assert "SQL inventory" in host.deploy(dry_run=True)["would_pause"]
    assert host.steps == [] and host.paused() is None and not (host.state / "deploy.json").exists()


# spec: ANYM-766
def test_a_halt_adds_its_code_actor_and_next_on_one_line_mode_0600_and_a_dry_run_is_unchanged(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    host.changes = {NEWEST: ["services/messaging/messaging_service/migrations/036_synthetic.sql"]}
    planned = host.deploy(dry_run=True)
    assert not p.PAUSE.exists() and not (host.state / "deploy.json").exists()
    outcome = host.deploy()
    raw = p.PAUSE.read_text()
    assert raw.endswith("}\n") and raw.count("\n") == 1 and stat.S_IMODE(p.PAUSE.stat().st_mode) == 0o600
    assert json.loads(raw) == outcome and outcome["reason"].startswith("the SQL inventory changed (")
    assert planned == {"status": "plan", "would_pause": outcome["reason"], "sha": NEWEST}
    actor, step = p.HALTS["migration_by_hand"]
    assert (outcome["code"], outcome["actor"], outcome["next"]) == ("migration_by_hand", actor, step)


# spec: ANYM-766
def test_a_code_missing_from_the_table_writes_agent_and_the_halts_anchor(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    host.changes = {NEWEST: ["services/messaging/messaging_service/migrations/036_synthetic.sql"]}
    monkeypatch.setattr(p, "HALTS", {})
    written = host.deploy()
    assert (written["code"], written["actor"], written["next"]) == (
        "migration_by_hand", "agent", "docs/operations/production.md#halts")


# spec: ANYM-766
def test_the_range_and_enrollment_halts_keep_their_reason_under_their_own_code(tmp_path, monkeypatch):
    path = "deploy/modules/recordings.json"
    host = Deployer(tmp_path, monkeypatch, changes={NEWEST: [path]})
    host.contents[f"{NEWEST}:{path}"] = MODULE_MANIFEST
    written = host.deploy(auto=True)
    assert written["code"] == "enroll_required" and written["reason"] == enroll_required(NEWEST)
    p.PAUSE.unlink()
    host.contents[f"{BASE}:{path}"], host.contents[f"{NEWEST}:{path}"] = MODULE_MANIFEST, "{not json"
    host.settings["projects"].append({"name": "deedkit-recordings", "directory": str(tmp_path), "files": [],
                                      "services": {"recordings-api": "recordings"}})
    host.current(BASE)
    assert host.deploy(auto=True)["code"] == "manifest_invalid"
    p.PAUSE.unlink()
    host.changes = {NEWEST: ["scripts/production.py"]}
    host.modified = {"scripts/production.py"}
    assert host.deploy(auto=True)["code"] == "tool_by_hand"


# spec: ANYM-766
def test_autodeploys_own_pause_read_keeps_working_with_the_new_keys(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    host.changes = {NEWEST: ["services/messaging/messaging_service/migrations/036_synthetic.sql"]}
    host.deploy()
    steps = list(host.steps)
    assert host.deploy() == {"status": "paused", "reason": p.PAUSE.read_text()[:1000].strip()}
    assert host.deploy(dry_run=True)["status"] == "paused" and host.steps == steps


def test_the_command_line_runs_deploy_only_as_the_host_operator_and_exits_1_on_a_new_pause(tmp_path, monkeypatch,
                                                                                            capsys):
    host = Deployer(tmp_path, monkeypatch, changes={NEWER: ["scripts/production.py"]})
    arguments = ["deploy", "--config", "plan-a.json", "--http-config", "plan-b.json"]
    monkeypatch.setattr(p.os, "geteuid", lambda: 1000)
    with pytest.raises(SystemExit):
        p.main(arguments)
    assert "host operator" in capsys.readouterr().err and host.git == []
    monkeypatch.setattr(p.os, "geteuid", lambda: 0)
    # The synthetic state directories belong to the test user, not to root.
    monkeypatch.setattr(p, "lock", lambda state: contextlib.nullcontext())
    monkeypatch.setattr(host.s, "locks", lambda settings: contextlib.nullcontext())
    monkeypatch.setattr(p, "config", lambda name: host.settings if name == "plan-a.json" else host.http)
    monkeypatch.setattr(host.s, "validate_settings", lambda value: value)
    monkeypatch.setattr(p, "published", lambda target, sha: host.attested(sha))
    with pytest.raises(SystemExit) as stopped:
        p.main(arguments)
    printed = json.loads(capsys.readouterr().out)
    assert stopped.value.code == 1 and printed["status"] == "halted" and "scripts/production.py" in printed["reason"]
    p.main(arguments)  # paused now: one quiet line, exit 0
    assert json.loads(capsys.readouterr().out)["status"] == "paused"


def autodeploy_unit():
    """The unit's `key=value` lines, the Environment= assignments as their own keys."""
    unit = (ROOT / "deploy/deedkit-autodeploy.service").read_text()
    lines = dict(line.split("=", 1) for line in unit.splitlines() if "=" in line and not line.startswith("#"))
    lines.update(line.split("=", 1)[1].split("=", 1) for line in unit.splitlines() if line.startswith("Environment="))
    return lines


def systemd_shell(text):
    """A unit command line as sh sees it: systemd turns $$ into $ and %% into %."""
    return text.replace("$$", "$").replace("%%", "%")


def admission_skips(bin_dir, backup, hour, *, systemctl_fails=False, date_fails=False):
    """Run the unit's ExecCondition with fake `systemctl` and `date` answers; True when it skips the run."""
    script = systemd_shell(re.fullmatch(r"/bin/sh -c '(.*)'", autodeploy_unit()["ExecCondition"]).group(1))
    bin_dir.mkdir(exist_ok=True)
    reads = {"systemctl": ("show -P ActiveState personal-os-backup.service", backup, systemctl_fails),
             "date": ("-u +%H", hour, date_fails)}
    for name, (arguments, answer, fails) in reads.items():
        (bin_dir / name).write_text(f"#!/bin/sh\n[ \"$*\" = '{arguments}' ] || exit 2\necho '{answer}'\n"
                                    + ("exit 1\n" if fails else ""))
        (bin_dir / name).chmod(0o755)
    return subprocess.run(["/bin/sh", "-c", script], env={"PATH": f"{bin_dir}:/usr/bin:/bin"}).returncode != 0


# spec: ANYM-407
def test_the_autodeploy_unit_runs_deploy_auto_at_any_hour_and_never_while_the_backup_unit_runs(tmp_path):
    # Owner 11-04: no clock window; the only exclusion is the backup itself (unit state here, runner lock below).
    lines = autodeploy_unit()
    timer = (ROOT / "deploy/deedkit-autodeploy.timer").read_text()
    assert "/usr/local/lib/deedkit/production.py deploy --auto " in lines["ExecStart"]
    assert "personal-os-backup.service" in lines["After"] and "OnUnitInactiveSec=1min" in timer
    assert "date" not in lines["ExecCondition"]

    def skipped(*arguments, **failures):
        return admission_skips(tmp_path / "bin", *arguments, **failures)

    assert not any(skipped(state, f"{hour:02d}") for state in ("inactive", "failed") for hour in range(24))
    assert skipped("activating", "12") and skipped("active", "17") and skipped("deactivating", "18")
    # A failed or unexpected read never admits a deploy.
    assert skipped("inactive", "12", systemctl_fails=True) and skipped("", "12") and skipped("garbage", "12")


# spec: ANYM-407
@pytest.mark.skipif(not Path("/usr/bin/flock").exists(), reason="needs util-linux flock, as on the host")
def test_a_deploy_and_a_backup_never_hold_the_backup_runner_lock_at_once(tmp_path):
    import fcntl
    import signal
    lines = autodeploy_unit()
    assert lines["DEEDKIT_BACKUP_LOCK"] == "/opt/personal-os-probe/backups/.runner.lock"
    assert lines["SuccessExitStatus"] == "75"
    prefix, deploy = systemd_shell(lines["ExecStart"]).split(" /usr/bin/python3 ", 1)
    assert deploy.startswith("/usr/local/lib/deedkit/production.py deploy --auto ")
    lock_file, marker = tmp_path / ".runner.lock", tmp_path / "deploy-ran"
    command = prefix.replace("${DEEDKIT_BACKUP_LOCK}", str(lock_file)).split()

    # A running backup holds its lock (backup_runner.py: flock LOCK_EX|LOCK_NB): the deploy is skipped with 75.
    backup = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(backup, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert subprocess.run([*command, "/bin/sh", "-c", f"touch {marker}"]).returncode == 75 and not marker.exists()
    finally:
        os.close(backup)

    # A running deploy holds the lock: a backup starting meanwhile refuses before it stops anything.
    running = subprocess.Popen([*command, "/bin/sh", "-c", f"touch {marker}; sleep 30"], start_new_session=True)
    backup = None
    try:
        for _ in range(200):
            if marker.exists():
                break
            subprocess.run(["sleep", "0.05"], check=True)
        backup = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
        with pytest.raises(BlockingIOError):
            fcntl.flock(backup, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        if backup is not None:
            os.close(backup)
        # flock does not forward signals to its command: stop the whole session it started.
        os.killpg(running.pid, signal.SIGTERM)
        running.wait()


# Promotion without the GitHub REST API (ANYM-406): main by `git ls-remote` and fetched history, CI by attestation.


def main_repository(tmp_path, monkeypatch, *, commits=3):
    """A synthetic GitHub: a local repository that real git reaches as `SOURCE`, with a main history and one commit off
    main. Returns (main commits, oldest first; the side commit)."""
    origin = tmp_path / "origin"
    environment = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1",
                   "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    def git(*arguments):
        return subprocess.run(["git", "-C", str(origin), *arguments], check=True, capture_output=True, text=True,
                              env=environment).stdout.strip()
    origin.mkdir()
    git("init", "--quiet", "--initial-branch=main")
    history = []
    for index in range(commits):
        git("commit", "--quiet", "--allow-empty", "-m", f"synthetic {index}")
        history.append(git("rev-parse", "HEAD"))
    git("checkout", "--quiet", "-b", "feature")
    git("commit", "--quiet", "--allow-empty", "-m", "synthetic side")
    side = git("rev-parse", "HEAD")
    git("checkout", "--quiet", "main")
    git("config", "uploadpack.allowFilter", "true")
    git("config", "uploadpack.allowAnySHA1InWant", "true")
    redirect(monkeypatch, "file://" + str(origin))
    return history, side


def redirect(monkeypatch, url):
    """production.py reads main from `url` instead of GitHub."""
    monkeypatch.setattr(p, "SOURCE", url)


def accepting_verifier(tmp_path, monkeypatch, attested=None):
    """The pinned verifier as a script: it reports a valid attestation for every commit, or only for `attested`."""
    known = "" if attested is None else (f'[ "$4" = "{attested}" ] || {{ echo "attest-verify: refused: manifest '
                                         f'deedkit-runtime:$4 not found" >&2; exit 1; }}\n')
    reference = p.REGISTRY + "runtime@" + MANIFEST
    return fake_verifier(tmp_path, monkeypatch, ("#!/bin/sh\n" + known + "printf '{\"reference\": \"%s\", \"digest\": \"%s\", "
                         "\"config_digest\": \"%s\", \"sha\": \"%s\"}' " + f"'{reference}' '{MANIFEST}' '{IMAGE}' \"$4\"\n").encode())


def offline(monkeypatch):
    """Any GitHub API request (the REST calls and /rate_limit all go through urlopen) fails the test."""
    requests = []
    def refuse(request, *args, **kwargs):
        requests.append(getattr(request, "full_url", request))
        raise AssertionError("a GitHub API request was made")
    monkeypatch.setattr(p.urllib.request, "urlopen", refuse)
    return requests


# spec: ANYM-406
def test_promotion_proves_a_commit_on_main_by_git_and_its_attestation_without_any_github_api_request(tmp_path,
                                                                                                     monkeypatch):
    history, _ = main_repository(tmp_path, monkeypatch)
    accepting_verifier(tmp_path, monkeypatch)
    requests = offline(monkeypatch)
    # The head and a commit main has moved past are both promotable; the evidence keeps the attested digests.
    for sha in (history[-1], history[0]):
        evidence = p.promotable(sha)
        assert evidence == {"sha": sha, "main": history[-1], "attestation": {
            "target": "runtime", "reference": p.REGISTRY + "runtime@" + MANIFEST, "digest": MANIFEST,
            "config_digest": IMAGE}}
        assert not any("run" in key or "url" in key for key in evidence)  # no run URL is invented
    assert requests == []


# spec: ANYM-406
def test_promotion_refuses_a_commit_main_does_not_contain_and_any_unreadable_or_ambiguous_main(tmp_path, monkeypatch):
    history, side = main_repository(tmp_path, monkeypatch)
    accepting_verifier(tmp_path, monkeypatch)
    offline(monkeypatch)
    verified = []
    real = p.published
    monkeypatch.setattr(p, "published", lambda target, sha: verified.append(sha) or real(target, sha))
    for sha in (side, "c" * 40):  # a pushed branch commit, and a commit the repository does not have
        with pytest.raises(p.Refused, match="not a commit on main"):
            p.promotable(sha)
    with pytest.raises(p.Refused, match="40-character"):
        p.promotable(history[-1][:12])
    assert verified == []  # refused before the verifier runs
    # `refs/heads/main` also names this tag's tail, so ls-remote prints two lines: refused, never the first one.
    subprocess.run(["git", "-C", str(tmp_path / "origin"), "tag", "refs/heads/main", history[0]], check=True,
                   capture_output=True)
    with pytest.raises(p.Unreachable, match="git ls-remote returned no main commit"):
        p.promotable(history[-1])
    redirect(monkeypatch, "file://" + str(tmp_path / "missing"))  # ls-remote fails
    with pytest.raises(p.Unreachable, match="main could not be read: git failed"):
        p.promotable(history[-1])
    assert verified == [] and p.transient(p.Unreachable("main could not be read"))


# spec: ANYM-406
def test_promotion_refuses_a_commit_without_a_valid_attestation_before_any_pull(tmp_path, monkeypatch):
    history, _ = main_repository(tmp_path, monkeypatch)
    offline(monkeypatch)
    # The verifier refuses a commit with no publication, or one signed for another SHA, ref or workflow (ANYM-301 B2).
    accepting_verifier(tmp_path, monkeypatch, attested=history[0])
    assert p.promotable(history[0])["attestation"]["digest"] == MANIFEST
    with pytest.raises(p.Unattested, match="attest-verify failed \\(exit 1\\): attest-verify: refused: manifest .* not found"):
        p.promotable(history[-1])
    # prepare runs that proof before it fetches, pulls or builds anything.
    (tmp_path / "host").mkdir()
    settings, calls, _ = fixture(tmp_path / "host", monkeypatch)
    monkeypatch.setattr(p, "promotable", lambda sha: (_ for _ in ()).throw(p.Unattested("attest-verify failed (exit 1)")))
    with pytest.raises(p.Unattested):
        p.prepare(settings, SHA)
    assert calls == [] and not (Path(settings["state_directory"]) / SHA).exists()


# spec: ANYM-406
def test_a_failed_rerun_after_the_images_were_attested_does_not_block_promotion(tmp_path, monkeypatch):
    history, _ = main_repository(tmp_path, monkeypatch)
    accepting_verifier(tmp_path, monkeypatch)
    # Whatever GitHub now says about the commit's runs (a failed rerun, an exhausted quota), promotion never asks.
    failed, _, _ = github(sha=history[-1], conclusion="failure")
    get = lambda path: {"status": "identical", "ahead_by": 0, "merge_base_commit": {"sha": history[-1]}} \
        if path.startswith("compare/") else failed(path)
    with pytest.raises(p.Refused, match="not a completed success"):
        p.verify(history[-1], get)  # the REST check of the release record refuses it
    requests = offline(monkeypatch)
    assert p.promotable(history[-1])["sha"] == history[-1] and requests == []


# spec: ANYM-406
@pytest.mark.parametrize("line, refusal", [
    ("manifest deedkit-runtime:" + SHA + " not found", p.Unattested),
    # Also what the pinned verifier prints when the referrers API failed: never proof of absence.
    ("no Sigstore bundle is attached to ghcr.io/gosync-inc/deedkit-runtime@" + MANIFEST, p.Unreachable),
    ("registry request failed: context deadline exceeded", p.Unreachable),
    ("anonymous registry token for deedkit-runtime: HTTP 503", p.Unreachable),
    ("manifest deedkit-runtime:" + SHA + ": HTTP 429", p.Unreachable),
    ("no attached bundle verified for ghcr.io/gosync-inc/deedkit-runtime@" + MANIFEST + ": bundle blob "
     + MANIFEST + ": HTTP 503", p.Unreachable),
    ("no attached bundle verified for ghcr.io/gosync-inc/deedkit-runtime@" + MANIFEST + ": registry read failed: "
     "unexpected EOF", p.Unreachable),
    ("no attached bundle verified for ghcr.io/gosync-inc/deedkit-runtime@" + MANIFEST + ": expected SourceRepositoryDigest",
     p.Refused),
    ("no attached bundle verified for ghcr.io/gosync-inc/deedkit-runtime@" + MANIFEST + ": manifest deedkit-runtime:"
     + SHA + " not found", p.Refused),
    ("manifest deedkit-runtime:" + SHA + ": Docker-Content-Digest sha256:1 differs from body digest sha256:2", p.Refused),
], ids=["not-published", "no-bundle-or-referrers-failed", "timeout", "token-unavailable", "rate-limited",
        "bundle-blob-unavailable", "bundle-read-failed", "wrong-commit", "nested-not-found", "digest-mismatch"])
def test_a_verifier_refusal_is_told_apart_as_not_published_unreachable_or_invalid(tmp_path, monkeypatch, line, refusal):
    fake_verifier(tmp_path, monkeypatch, f"#!/bin/sh\necho 'attest-verify: refused: {line}' 1>&2\nexit 1\n".encode())
    with pytest.raises(p.Refused) as refused:
        p.published("runtime", SHA)
    assert type(refused.value) is refusal and line in str(refused.value)
    fake_verifier(tmp_path, monkeypatch, b"#!/bin/sh\necho '{}'\n")  # an answer that verifies nothing
    with pytest.raises(p.Refused, match="no verified reference") as refused:
        p.published("runtime", SHA)
    assert type(refused.value) is p.Refused


# spec: ANYM-406
def test_deploy_never_takes_an_older_commit_while_ghcr_is_unreachable_and_pauses_on_the_third_run(tmp_path,
                                                                                                    monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    host.unreached.add(NEWEST)
    for count in (1, 2):
        outcome = host.deploy()
        assert outcome["status"] == "error" and "registry request failed" in outcome["reason"] and host.steps == []
        assert p.read(host.state / "deploy.json")["unreachable"]["count"] == count and host.paused() is None
    outcome = host.deploy()
    assert outcome["status"] == "halted" and "could not be read 3 times in a row" in outcome["reason"]
    assert host.steps == [] and host.paused() == outcome["reason"]
    # The count resets once a run reads GHCR again.
    p.PAUSE.unlink()
    host.unreached.clear()
    host.green = set()
    assert host.deploy()["status"] == "waiting" and "unreachable" not in p.read(host.state / "deploy.json")


# spec: ANYM-406
def test_git_or_ghcr_failing_inside_a_release_step_is_retried_and_the_third_run_in_a_row_pauses(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    def unreachable(sha):
        # Its first check failed before preparation started: nothing is left behind.
        (host.state / sha / "prepared.json").unlink()
        (host.state / sha).rmdir()
        raise p.Unreachable("main could not be read: git failed (exit 128): fatal: unable to access")
    host.fail_prepare = unreachable
    for count in (1, 2):
        outcome = host.deploy()
        assert outcome["status"] == "error" and outcome["unreachable"]["count"] == count and host.paused() is None
    outcome = host.deploy()
    assert outcome["status"] == "halted" and "could not be read 3 times in a row" in outcome["reason"]
    assert host.paused() == outcome["reason"]
    # A run that gets past the step starts counting again.
    p.PAUSE.unlink()
    host.fail_prepare = None
    assert host.deploy()["status"] == "deployed" and "unreachable" not in p.read(host.state / "deploy.json")


# spec: ANYM-406
def test_deploy_takes_a_commit_only_when_every_image_of_plans_a_and_b_is_attested(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    host.http["projects"] = [{**host.http["projects"][0], "services": {"api": "runtime", "gateway": "gateway"}}]
    p.write(host.http_state / "http-current.json", {"sha": BASE, "status": "serving", "config_digest": p.digest(host.http)})
    checked = []
    def published(target, sha):
        checked.append((target, sha))
        if sha == NEWEST and target == "gateway":  # runtime is attested, the gateway's publication still runs
            raise p.Unattested("attest-verify failed (exit 1): attest-verify: refused: manifest deedkit-gateway:x not found")
        return verified_image(target, IMAGE)
    monkeypatch.setattr(p, "published", published)
    outcome = p.deploy(host.settings, host.http)
    assert outcome["status"] == "deployed" and outcome["sha"] == NEWER
    assert ("gateway", NEWEST) in checked and {target for target, sha in checked if sha == NEWER} == {"runtime", "gateway"}
    # Evidence that does not verify is no publication in progress: it pauses before any step.
    (tmp_path / "invalid").mkdir()
    other = Deployer(tmp_path / "invalid", monkeypatch)
    monkeypatch.setattr(p, "published", lambda target, sha: (_ for _ in ()).throw(p.Refused(
        "attest-verify failed (exit 1): attest-verify: refused: no attached bundle verified for x: wrong signer")))
    outcome = p.deploy(other.settings, other.http)
    assert outcome["status"] == "halted" and "wrong signer" in outcome["reason"] and other.steps == []


# spec: ANYM-406
def test_promotion_git_ignores_the_operator_environment_and_global_configuration(tmp_path, monkeypatch):
    history, side = main_repository(tmp_path, monkeypatch)
    accepting_verifier(tmp_path, monkeypatch)
    offline(monkeypatch)
    # A repository whose main contains the side commit, reachable through GIT_DIR or a global or environment URL rewrite.
    other = tmp_path / "other.git"
    subprocess.run(["git", "clone", "--quiet", "--bare", str(tmp_path / "origin"), str(other)], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(other), "update-ref", "refs/heads/main", side], check=True, capture_output=True)
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(f'[url "file://{other}"]\n\tinsteadOf = {p.SOURCE}\n')
    for name, value in (("GIT_DIR", str(other)), ("HOME", str(home)), ("GIT_DEFAULT_HASH", "sha256"),
                        ("GIT_CONFIG_COUNT", "1"), ("GIT_CONFIG_KEY_0", f"url.file://{other}.insteadOf"),
                        ("GIT_CONFIG_VALUE_0", p.SOURCE)):
        monkeypatch.setenv(name, value)
    with pytest.raises(p.Refused, match="not a commit on main"):
        p.promotable(side)
    assert p.promotable(history[-1])["main"] == history[-1]


# spec: ANYM-406
def test_promotion_git_ignores_the_configuration_of_the_repository_it_runs_in(tmp_path, monkeypatch):
    history, side = main_repository(tmp_path, monkeypatch)
    accepting_verifier(tmp_path, monkeypatch)
    offline(monkeypatch)
    # The operator runs production.py from a checkout whose local config rewrites the source to a main with the side
    # commit on it.
    other = tmp_path / "other.git"
    subprocess.run(["git", "clone", "--quiet", "--bare", str(tmp_path / "origin"), str(other)], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(other), "update-ref", "refs/heads/main", side], check=True, capture_output=True)
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "init", "--quiet", str(checkout)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(checkout), "config", f"url.file://{other}.insteadOf", p.SOURCE], check=True,
                   capture_output=True)
    monkeypatch.chdir(checkout)
    with pytest.raises(p.Refused, match="not a commit on main"):
        p.promotable(side)
    assert p.promotable(history[-1])["main"] == history[-1]


# spec: ANYM-439
def test_deploy_removes_old_release_images_once_and_only_after_it_ends_deployed(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    assert host.deploy(dry_run=True)["status"] == "plan" and host.retained == []
    outcome = host.deploy()
    # Under the deploy's own locks, after the deployed receipt; plan B's and the host state's locks are held already.
    assert outcome["status"] == "deployed" and host.retained == [(host.http_state, host.s.GLOBAL_LOCK)]
    assert outcome["image_removal"] == {"removed": [], "failed": [], "kept": 2}
    assert p.read(host.state / "deploy.json") == outcome
    assert host.deploy()["status"] == "current" and len(host.retained) == 1


# spec: ANYM-439
def test_a_deploy_halted_after_plan_a_removes_no_images(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    host.fail_seamless_promote = lambda sha: (_ for _ in ()).throw(p.Refused("candidate HTTP readiness did not pass"))
    outcome = host.deploy()
    assert outcome["status"] == "halted" and host.retained == [] and "image_removal" not in outcome


# spec: ANYM-439
@pytest.mark.parametrize("failure", ["removal", "summary write"])
def test_a_failed_image_removal_or_summary_leaves_the_deploy_deployed_without_a_pause(tmp_path, monkeypatch, failure):
    host = Deployer(tmp_path, monkeypatch)
    if failure == "removal":
        monkeypatch.setattr(p, "retain_images", RETAIN_IMAGES)
        monkeypatch.setattr(p, "remove_images", lambda settings, held=(): (_ for _ in ()).throw(RuntimeError()))
    else:
        write = p.write
        def full(path, value):
            if path.name == "deploy.json" and "image_removal" in value:
                raise OSError("No space left on device")
            write(path, value)
        monkeypatch.setattr(p, "write", full)
    outcome = host.deploy()
    receipt = p.read(host.state / "deploy.json")
    assert outcome["status"] == receipt["status"] == "deployed" and host.paused() is None
    if failure == "removal":
        assert outcome["image_removal"] == {"skipped": "RuntimeError"} and receipt == outcome
    else:
        assert outcome["image_removal"]["receipt"].startswith("not written") and "image_removal" not in receipt


# spec: ANYM-689
def test_deploy_cleans_retired_containers_then_images_before_any_release_step_and_records_one_line(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    seen = []
    def containers(settings, remove):
        seen.append(("containers", remove, list(host.steps)))
        return {"removed": ["a/api", "a/gateway"], "generations": ["a"], "skipped": [], "unknown": [], "unresolved": 0}
    monkeypatch.setattr(host.s, "clean", containers)
    def images(settings, *, held=(), **extra):
        seen.append(("preflight images" if extra else "final images", sorted(extra), list(host.steps)))
        return {"removed": [{"image": "x"}], "failed": [], "kept": 3}
    monkeypatch.setattr(p, "retain_images", images)
    outcome = host.deploy()
    assert outcome["status"] == "deployed"
    # Containers first, then the image pass with a deadline and the keep reasons, both before the first release step.
    assert seen[0] == ("containers", True, []) and seen[1] == ("preflight images", ["deadline", "reasons"], [])
    assert seen[2][0] == "final images" and seen[2][1] == [] and host.steps
    assert outcome["cleanup"]["containers_removed"] == 2 and outcome["cleanup"]["generations"] == ["a"]
    assert outcome["cleanup"]["images_removed"] == 2 and "disk_free_after_gib" in outcome["cleanup"]
    assert p.read(host.state / "deploy.json")["cleanup"] == outcome["cleanup"]


# spec: ANYM-689
def test_a_dry_run_and_a_current_host_do_not_run_the_preflight(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch, history=())
    monkeypatch.setattr(host.s, "clean", lambda *a: pytest.fail("cleanup ran"))
    assert host.deploy()["status"] == "current" and host.preflight == []
    host = Deployer(tmp_path / "again", monkeypatch) if (tmp_path / "again").mkdir() is None else None
    monkeypatch.setattr(host.s, "clean", lambda *a: pytest.fail("cleanup ran"))
    assert host.deploy(dry_run=True)["status"] == "plan" and host.preflight == []


# spec: ANYM-689
@pytest.mark.parametrize("failure", ["containers", "images", "both"])
def test_a_failing_preflight_never_changes_the_deploy_outcome(tmp_path, monkeypatch, failure):
    host = Deployer(tmp_path, monkeypatch)
    if failure in {"containers", "both"}:
        monkeypatch.setattr(host.s, "clean", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    if failure in {"images", "both"}:
        monkeypatch.setattr(p, "retain_images", lambda settings, *, held=(), **extra: {"skipped": "RuntimeError"})
    outcome = host.deploy()
    assert outcome["status"] == "deployed" and host.paused() is None
    assert outcome["preflight"]["containers"] == ({"error": "RuntimeError"} if failure != "images" else outcome["preflight"]["containers"])


# spec: ANYM-689
def test_the_preflight_image_pass_lifts_a_host_under_the_gate_that_container_removal_alone_cannot(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    free = {"bytes": p.MIN_FREE_BYTES - 1}
    def prepare_checks_space(sha):
        if free["bytes"] < p.MIN_FREE_BYTES:
            raise p.Refused("at least 15 GiB free is required on /var/lib/containerd before obtaining production images")
    host.fail_prepare = prepare_checks_space
    # The fake credits bytes to the image removal only: stopped containers hold no bytes, their images do.
    monkeypatch.setattr(host.s, "clean", lambda *a: {"removed": ["a/api"], "generations": ["a"]})
    def images(settings, *, held=(), **extra):
        if extra:
            free["bytes"] += 20 * 1024**3
        return {"removed": [], "failed": [], "kept": 1, **({"kept_reasons": {"used by container": 1}} if extra else {})}
    monkeypatch.setattr(p, "retain_images", images)
    assert host.deploy()["status"] == "deployed"
    # Without the image pass the same host halts, and deploy.json names why the images stay.
    host = Deployer(tmp_path / "halted", monkeypatch) if (tmp_path / "halted").mkdir() is None else None
    free["bytes"] = p.MIN_FREE_BYTES - 1
    host.fail_prepare = prepare_checks_space
    monkeypatch.setattr(host.s, "clean", lambda *a: {"removed": ["a/api"], "generations": ["a"]})
    monkeypatch.setattr(p, "retain_images", lambda settings, *, held=(), **extra: {
        "removed": [], "failed": [], "kept": 4, **({"kept_reasons": {"prepared release <id> is not applied": 4}} if extra else {})})
    host.deploy()
    pause = p.read(host.state / "deploy.json")
    assert pause["status"] == "halted" and "15 GiB" in pause["reason"]
    assert pause["preflight"]["images"]["kept_reasons"] == {"prepared release <id> is not applied": 4}


# spec: ANYM-689
def test_the_retirement_of_the_predecessor_in_this_run_is_counted_in_the_one_cleanup_line(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    monkeypatch.setattr(host.s, "clean", lambda *a: {"removed": ["a/api"], "generations": ["a"]})
    promote = host.s.promote
    monkeypatch.setattr(host.s, "promote", lambda settings, sha: {**promote(settings, sha), "status": "serving",
                        "cleanup": {"removed": ["b/api", "b/gateway"], "generations": ["b", "a"]}})
    outcome = host.deploy()
    assert outcome["status"] == "deployed"
    assert outcome["cleanup"]["containers_removed"] == 3 and outcome["cleanup"]["generations"] == ["a", "b"]
    assert p.read(host.state / "deploy.json")["cleanup"] == outcome["cleanup"]


# spec: ANYM-689
@pytest.mark.parametrize("stage", ["plan A", "plan B"])
def test_a_halted_deploy_keeps_the_cleanup_summary_and_its_own_reason(tmp_path, monkeypatch, stage):
    host = Deployer(tmp_path, monkeypatch)
    monkeypatch.setattr(host.s, "clean", lambda *a: {"removed": ["a/api"], "generations": ["a"]})
    def refuse(sha):
        raise p.Refused("synthetic refusal")
    setattr(host, "fail_prepare" if stage == "plan A" else "fail_seamless_promote", refuse)
    host.deploy()
    receipt = p.read(host.state / "deploy.json")
    assert receipt["status"] == "halted" and "synthetic refusal" in receipt["reason"] and "synthetic refusal" in host.paused()
    assert receipt["cleanup"]["containers_removed"] == 1 and receipt["preflight"]["containers"]["generations"] == ["a"]


# spec: ANYM-689
def test_a_failed_write_of_the_cleanup_summary_leaves_the_halt_receipt_and_the_pause_reason(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    monkeypatch.setattr(host.s, "clean", lambda *a: {"removed": ["a/api"], "generations": ["a"]})
    def refuse(sha):
        raise p.Refused("synthetic refusal")
    host.fail_prepare = refuse
    write = p.write
    def full(target, value):
        if target.name == "deploy.json" and "cleanup" in value:
            raise OSError("No space left on device")
        write(target, value)
    monkeypatch.setattr(p, "write", full)
    outcome = host.deploy()
    receipt = p.read(host.state / "deploy.json")
    assert outcome["status"] == receipt["status"] == "halted" and "synthetic refusal" in receipt["reason"]
    assert "cleanup" not in receipt and "synthetic refusal" in host.paused()


# spec: ANYM-689
def test_the_preflight_budget_bounds_every_command_and_is_always_reset(tmp_path, monkeypatch):
    seen = []
    def fake(argv, **kwargs):
        seen.append(kwargs["timeout"])
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(p.subprocess, "run", fake)
    assert p.run(["true"]) == "" and seen == [1800]
    monkeypatch.setattr(p, "DEADLINE", p.time.monotonic() + 60)
    p.run(["true"])
    assert 0 < seen[-1] <= 60
    monkeypatch.setattr(p, "DEADLINE", p.time.monotonic() - 1)
    with pytest.raises(p.Refused, match="time budget"):
        p.run(["true"])
    assert len(seen) == 2
    monkeypatch.setattr(p, "DEADLINE", None)
    # The pass sets the deadline for both stages and clears it whether they end or raise.
    during = []
    stage = SimpleNamespace(GLOBAL_LOCK=tmp_path, clean=lambda http, remove: during.append(p.DEADLINE) or {"removed": []})
    monkeypatch.setattr(p, "retain_images", lambda settings, **extra: during.append(p.DEADLINE) or {"removed": []})
    p.preflight_cleanup({}, {}, stage, tmp_path)
    assert len(during) == 2 and all(value is not None for value in during) and p.DEADLINE is None
    monkeypatch.setattr(p, "retain_images", lambda settings, **extra: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        p.preflight_cleanup({}, {}, stage, tmp_path)
    assert p.DEADLINE is None


# spec: ANYM-689
def test_the_image_pass_starts_no_discovery_after_its_deadline(tmp_path, monkeypatch):
    settings = retention_settings(tmp_path)
    store = Images(monkeypatch, [runtime("1")])
    summary = p.remove_images(settings, deadline=p.time.monotonic() - 1)
    assert summary["interrupted"] == "time budget" and summary["removed"] == [] and store.calls == []


class RetiredHost(Images):
    """`Images` plus the HTTP generations' exited containers: `docker inspect <id>` answers the full object, `docker rm
    <id>` deletes it (so its image is no longer in use) and the labelled listing shows what is left."""

    def __init__(self, monkeypatch, images, detail):
        super().__init__(monkeypatch, images, {identity: item["Image"] for identity, item in detail.items()})
        self.detail = detail

    def run(self, argv, **kwargs):
        if argv[:2] == ["docker", "inspect"] and len(argv) == 3 and argv[2] in self.detail:
            self.calls.append(argv)
            return json.dumps([self.detail[argv[2]]])
        if argv[:2] == ["docker", "rm"] and len(argv) == 3:
            self.calls.append(argv)
            del self.detail[argv[2]], self.containers[argv[2]]
            return ""
        if argv[:2] == ["docker", "ps"] and any("ai.deedkit.http-release" in part for part in argv):
            self.calls.append(argv)
            return "".join(f"{identity} {item['Config']['Labels']['ai.deedkit.http-release']} {item['State']['Status']}\n"
                           for identity, item in self.detail.items())
        return super().run(argv, **kwargs)


def http_generations(state, images, *, serving=None, digest=None):
    """HTTP generations as `retire` leaves them: each retired by its successor's receipt, the last one serving (with the
    revision `serving` and the configuration digest `digest` when given)."""
    state.mkdir(mode=0o700, exist_ok=True)
    detail, previous = {}, None
    for number, image in enumerate(images):
        generation, sha = "%012x" % (number + 1), "%040x" % (number + 1)
        if serving:
            # Older revisions sit before the serving one on main, as `unresolved` requires of every serving attempt.
            sha = serving if number == len(images) - 1 else "%040x" % (0xa0 + number)
        identities = {role: {"container": "%064x" % (0x100 + number * 2 + (role == "gateway")), "image": image}
                      for role in ("api", "gateway")}
        receipt = {"sha": sha, "generation": generation, "status": "serving", "containers": identities,
                   "retired_roles": ["gateway", "api"] if previous else [], "previous": previous,
                   **({"config_digest": digest} if digest else {})}
        (state / sha).mkdir(exist_ok=True)
        p.write(state / sha / "http-result.json", receipt)
        p.write(state / sha / "http-attempted.json", {"status": "serving", "sha": sha})
        p.write(state / "http-current.json", receipt)
        for role, identity in identities.items():
            detail[identity["container"]] = {"Id": identity["container"], "Image": image,
                "State": {"Status": "running" if number == len(images) - 1 else "exited", "Running": number == len(images) - 1},
                "Config": {"Labels": {"com.docker.compose.project": "deedkit-http-" + generation,
                                      "com.docker.compose.service": f"http-{role}-{generation}",
                                      "ai.deedkit.http-release": generation}}}
        previous = {**receipt, "previous": None}
    return state, detail


# spec: ANYM-689
def test_the_preflight_removes_retired_containers_and_then_the_image_they_pinned(tmp_path, monkeypatch):
    from scripts import seamless_release as s
    settings = retention_settings(tmp_path)
    state = Path(settings["state_directory"])
    for seed, before in (("1", OLD_IMAGE), ("2", image_id("1")), ("3", image_id("2"))):
        released(state, seed * 40, image_id(seed), before)
    (tmp_path / "global").mkdir()
    monkeypatch.setattr(s, "GLOBAL_LOCK", tmp_path / "global")
    http_state, detail = http_generations(tmp_path / "http-state", [image_id("1"), image_id("3"), image_id("3")])
    http = {"state_directory": str(http_state)}
    store = RetiredHost(monkeypatch, [runtime(seed) for seed in "123"], detail)
    # Before: the exited containers of the retired generation keep its image ("used by container").
    assert candidates(settings) == [] and image_id("1") in store.references
    result = p.preflight_cleanup(settings, http, s, http_state)
    assert sorted(result["containers"]["removed"]) == ["000000000001/api", "000000000001/gateway"]
    assert [item["image"] for item in result["images"]["removed"]] == ["1" * 12] and result["images"]["failed"] == []
    assert set(store.references) == {image_id("2"), image_id("3")} and len(store.detail) == 4
    # The order is the point: both containers go before the image removal is even attempted.
    kinds = ["rm" if call[1] == "rm" and len(call) == 3 else "image" for call in store.calls if call[1] == "rm" or call[:3] == ["docker", "image", "rm"]]
    assert kinds == ["rm", "rm", "image"] and p.DEADLINE is None
    assert p.cleanup_line(result, None)["containers_removed"] == 2


class PruneHost(Images):
    """`Images` plus main's history and the bind mounts of the containers."""

    def __init__(self, monkeypatch, images, history, mounts=()):
        super().__init__(monkeypatch, images)
        self.history, self.mounts = history, mounts

    def run(self, argv, **kwargs):
        if "rev-list" in argv:
            if self.history is None:
                raise p.Refused("git failed")
            return "\n".join(self.history)
        if argv[:2] == ["docker", "ps"] and self.mounts:
            return "mounting\n"
        if argv[:2] == ["docker", "inspect"] and self.mounts:
            return json.dumps([{"Id": "mounting", "Image": image_id("f"), "Mounts": [{"Source": source} for source in self.mounts]}])
        return super().run(argv, **kwargs)


def prune_host(tmp_path):
    """Nine applied plan A releases, seed 9 current and seed 1 oldest."""
    settings = retention_settings(tmp_path)
    state = Path(settings["state_directory"])
    seeds = "123456789"
    before = OLD_IMAGE
    for seed in seeds:
        released(state, seed * 40, image_id(seed), before)
        before = image_id(seed)
    return settings, state, [seed * 40 for seed in reversed(seeds)]


# spec: ANYM-689
def test_old_release_directories_go_and_everything_a_rollback_or_a_candidate_needs_stays(tmp_path, monkeypatch):
    settings, state, history = prune_host(tmp_path)
    http_state = tmp_path / "http"
    http_state.mkdir()
    (state / ("c" * 40)).mkdir()  # newer than the current release: not in its history
    (state / ("d" * 40)).mkdir()  # a hotfix never on main
    p.write(state / ("2" * 40) / "attempted.json", {"status": "applying"})  # unfinished once its result is gone
    (state / ("2" * 40) / "result.json").unlink()
    PruneHost(monkeypatch, [], history, mounts=[f"{state}/{'3' * 40}/api"])
    summary = p.prune_releases(settings, http_state, p.time.monotonic() + 60)
    assert summary == {"removed": ["4" * 12, "1" * 12], "failed": []}
    left = {path.name for path in state.iterdir() if p.RELEASE_DIRECTORY.fullmatch(path.name)}
    assert left == {seed * 40 for seed in "98765" "23" "cd"}
    assert p.unfinished(state) == ["2" * 40]


# spec: ANYM-689
def test_a_never_applied_preparation_older_than_the_current_release_stops_pinning_its_images(tmp_path, monkeypatch):
    settings, state, history = prune_host(tmp_path)
    (state / ("1" * 40) / "result.json").unlink()  # prepared, never applied
    store = PruneHost(monkeypatch, [runtime("1"), runtime("9")], history)
    assert candidates(settings) == []
    (tmp_path / "global").mkdir()
    (tmp_path / "http").mkdir()
    stage = SimpleNamespace(GLOBAL_LOCK=tmp_path / "global", clean=lambda *a: {"removed": []})
    result = p.preflight_cleanup(settings, {}, stage, tmp_path / "http")
    assert "1" * 12 in result["releases"]["removed"]
    assert [item["image"] for item in result["images"]["removed"]] == ["1" * 12] and image_id("1") not in store.references
    assert p.cleanup_line(result, None)["releases_removed"] == len(result["releases"]["removed"])


# spec: ANYM-689
def test_a_release_removal_cut_short_never_reads_as_an_unfinished_apply_and_the_next_pass_finishes_it(tmp_path, monkeypatch):
    settings, state, history = prune_host(tmp_path)
    (tmp_path / "http").mkdir()
    PruneHost(monkeypatch, [], history)
    p.write(state / ("1" * 40) / "attempted.json", {"status": "applied"})
    remove = p.remove_path
    def cut_short(path, deadline):
        (Path(path) / "result.json").unlink()
        raise p.subprocess.TimeoutExpired("rm", 1)
    monkeypatch.setattr(p, "remove_path", cut_short)
    summary = p.prune_releases(settings, tmp_path / "http", p.time.monotonic() + 60)
    assert summary["failed"][0]["reason"] == "TimeoutExpired" and p.unfinished(state) == []
    assert (state / p.RELEASE_TRASH / ("1" * 40)).is_dir()
    monkeypatch.setattr(p, "remove_path", remove)
    p.prune_releases(settings, tmp_path / "http", p.time.monotonic() + 60)
    assert list((state / p.RELEASE_TRASH).iterdir()) == []


# spec: ANYM-689
def test_the_release_pass_stops_at_its_deadline_and_leaves_the_trash_for_the_next_pass(tmp_path, monkeypatch):
    settings, state, history = prune_host(tmp_path)
    (tmp_path / "http").mkdir()
    (state / p.RELEASE_TRASH / ("1" * 40)).mkdir(parents=True)
    PruneHost(monkeypatch, [], history)
    summary = p.prune_releases(settings, tmp_path / "http", p.time.monotonic() - 1)
    assert summary["interrupted"] == "time budget" and summary["removed"] == []
    assert (state / p.RELEASE_TRASH / ("1" * 40)).is_dir() and (state / ("1" * 40)).is_dir()


# spec: ANYM-689
def test_a_failed_apply_not_rolled_back_keeps_its_directory_for_the_rollback(tmp_path, monkeypatch):
    settings, state, history = prune_host(tmp_path)
    (tmp_path / "http").mkdir()
    p.write(state / ("1" * 40) / "result.json", {"sha": "1" * 40, "status": "unhealthy"})
    PruneHost(monkeypatch, [], history)
    assert "1" * 12 not in p.prune_releases(settings, tmp_path / "http", p.time.monotonic() + 60)["removed"]
    assert (state / ("1" * 40) / "synthetic.before.json").exists()
    p.write(state / ("1" * 40) / "rollback.json", {"status": "rolled-back"})
    assert "1" * 12 in p.prune_releases(settings, tmp_path / "http", p.time.monotonic() + 60)["removed"]


# spec: ANYM-689
def test_an_old_commit_prepared_since_the_current_release_stays_because_it_can_still_apply(tmp_path, monkeypatch):
    settings, state, history = prune_host(tmp_path)
    (tmp_path / "http").mkdir()
    prepared = state / ("1" * 40) / "prepared.json"
    (state / ("1" * 40) / "result.json").unlink()
    bound = p.read(prepared)
    p.write(prepared, {**bound, "current_digest": p.digest(p.read(state / "current.json"))})
    PruneHost(monkeypatch, [], history)
    assert "1" * 12 not in p.prune_releases(settings, tmp_path / "http", p.time.monotonic() + 60)["removed"]
    p.write(prepared, {**bound, "current_digest": "another release"})
    assert "1" * 12 in p.prune_releases(settings, tmp_path / "http", p.time.monotonic() + 60)["removed"]
    # A receipt from before `current_digest`: its age against current.json decides.
    release = tmp_path / "state" / ("6" * 40)
    (release / "result.json").unlink()
    newer = (state / "current.json").stat().st_mtime + 5
    p.os.utime(release / "prepared.json", (newer, newer))
    assert p.release_in_use(release, set(), p.read(state / "current.json"))
    p.os.utime(release / "prepared.json", (newer - 10, newer - 10))
    assert not p.release_in_use(release, set(), p.read(state / "current.json"))


# spec: ANYM-689
def test_an_http_preparation_stays_while_the_generation_it_names_serves(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    p.write(release / "http-prepared.json", {"previous": {"generation": "a" * 12}})
    assert p.release_in_use(release, set(), None, {"generation": "a" * 12})
    assert not p.release_in_use(release, set(), None, {"generation": "b" * 12})


# spec: ANYM-689
def test_the_release_pass_removes_nothing_when_history_or_settlement_is_unknown(tmp_path, monkeypatch):
    settings, state, history = prune_host(tmp_path)
    http_state = tmp_path / "http"
    http_state.mkdir()
    count = len(list(state.iterdir()))
    PruneHost(monkeypatch, [], None)
    assert p.prune_releases(settings, http_state, p.time.monotonic() + 60)["removed"] == []
    assert len(list(state.iterdir())) == count
    PruneHost(monkeypatch, [], history[1:])  # the current release is not on main
    assert p.prune_releases(settings, http_state, p.time.monotonic() + 60)["error"] == "Refused"
    p.write(state / "current.json", {**p.read(state / "current.json"), "status": "rolled-back"})
    PruneHost(monkeypatch, [], history)
    assert p.prune_releases(settings, http_state, p.time.monotonic() + 60)["removed"] == []
    assert p.prune_releases({}, http_state, p.time.monotonic() + 60)["error"] == "KeyError"


# spec: ANYM-689
@pytest.mark.parametrize("copy", [False, True], ids=["configuration", "undo_copy"])
def test_a_release_a_backup_runner_configuration_names_stays_with_no_backup_container(tmp_path, monkeypatch, copy):
    settings, state, history = prune_host(tmp_path)
    http_state = tmp_path / "http"
    http_state.mkdir()
    runners = tmp_path / "runners"
    configs = tuple(runners / name / "runner.json" for name in ("messaging", "calendar"))
    monkeypatch.setattr(p, "RUNNER_CONFIGS", configs)
    mounts = [{"name": "settings", "source": f"{state}/{'1' * 40}/settings"}]
    pin(configs[0], image_id("a"), **({} if copy else {"mounts": mounts}))
    if copy:
        (runners / "messaging" / "runner.json.before-x").write_text(json.dumps({"image": image_id("a"), "mounts": mounts}))
    PruneHost(monkeypatch, [], history)
    summary = p.prune_releases(settings, http_state, p.time.monotonic() + 60)
    assert summary == {"removed": ["4" * 12, "3" * 12, "2" * 12], "failed": []} and (state / ("1" * 40)).is_dir()


# spec: ANYM-689
@pytest.mark.parametrize("undo", [False, True], ids=["configuration", "undo_copy"])
def test_a_directory_in_the_trash_a_runner_configuration_names_stays(tmp_path, monkeypatch, undo):
    settings, state, history = prune_host(tmp_path)
    (tmp_path / "http").mkdir()
    trash = state / p.RELEASE_TRASH
    (trash / ("e" * 40) / "settings").mkdir(parents=True)
    (trash / ("f" * 40)).mkdir()
    config = tmp_path / "runners" / "runner.json"
    monkeypatch.setattr(p, "RUNNER_CONFIGS", (config,))
    source = f"{trash}/{'e' * 40}/settings"
    pin(config, image_id("a"), **({} if undo else {"source": source}))
    if undo:
        (config.parent / "runner.json.before-x").write_text(json.dumps({"image": image_id("a"), "source": source}))
    PruneHost(monkeypatch, [], history)
    p.prune_releases(settings, tmp_path / "http", p.time.monotonic() + 60)
    assert (trash / ("e" * 40) / "settings").is_dir() and not (trash / ("f" * 40)).exists()


# spec: ANYM-689
def test_a_release_a_runner_configuration_names_stays_in_the_http_state_too(tmp_path, monkeypatch):
    settings, state, history = prune_host(tmp_path)
    http_state = tmp_path / "http"
    for sha in history:
        (http_state / sha).mkdir(parents=True)
    p.write(http_state / "http-current.json", {"status": "serving", "sha": history[0], "generation": "a" * 12})
    config = tmp_path / "runners" / "runner.json"
    monkeypatch.setattr(p, "RUNNER_CONFIGS", (config,))
    pin(config, image_id("a"), source=f"{http_state}/{history[-1]}/settings")
    PruneHost(monkeypatch, [], history)
    p.prune_releases(settings, http_state, p.time.monotonic() + 60)
    assert (http_state / history[-1]).is_dir() and not (http_state / history[-2]).exists()


# spec: ANYM-689
@pytest.mark.parametrize("content", ["{broken", "[]"], ids=["broken", "not_a_mapping"])
def test_a_runner_configuration_that_cannot_be_read_prunes_nothing_and_says_so(tmp_path, monkeypatch, content):
    settings, state, history = prune_host(tmp_path)
    (tmp_path / "http").mkdir()
    (state / p.RELEASE_TRASH / ("e" * 40)).mkdir(parents=True)
    config = tmp_path / "runners" / "runner.json"
    config.parent.mkdir()
    config.write_text(content)
    monkeypatch.setattr(p, "RUNNER_CONFIGS", (config,))
    PruneHost(monkeypatch, [], history)
    summary = p.prune_releases(settings, tmp_path / "http", p.time.monotonic() + 60)
    assert summary["removed"] == [] and summary["error"] == "Refused" and str(config) in summary["skipped"]
    assert (state / ("1" * 40)).is_dir() and (state / p.RELEASE_TRASH / ("e" * 40)).is_dir()


# spec: ANYM-689
def test_the_receipt_that_records_a_retirement_stays_while_that_generations_containers_exist(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    p.write(release / "http-attempted.json", {"status": "serving"})
    p.write(release / "http-result.json", {"previous": {"sha": "a" * 40}})
    assert p.release_in_use(release, {"a" * 40}) and not p.release_in_use(release, {"b" * 40})
    p.write(release / "http-attempted.json", {"status": "recovery-required"})
    assert p.release_in_use(release, set())
    (release / "http-attempted.json").write_text("{broken")
    assert p.release_in_use(release, set())


SHAS = st.sampled_from([f"{number:040x}" for number in range(30)])


# spec: ANYM-689
@given(st.lists(SHAS, unique=True), st.sets(SHAS), st.sets(SHAS), st.integers(0, 8))
def test_the_release_selector_never_picks_a_protected_newest_or_off_history_directory(history, present, protected, keep):
    chosen = p.prunable_releases(history, present, protected, keep)
    ordered = [sha for sha in history if sha in present]
    assert set(chosen) <= set(ordered) - set(ordered[:keep]) - protected
    assert set(ordered) - set(chosen) >= set(ordered[:keep]) | (protected & set(ordered))
    # Idempotent: a second pass over what is left removes nothing more.
    assert p.prunable_releases(history, present - set(chosen), protected, keep) == []


# spec: ANYM-439
def test_the_containerd_store_keeps_the_image_floor_on_its_disk_and_the_host_floor_on_dockers_root(tmp_path, monkeypatch):
    free = {}
    monkeypatch.setattr(p.shutil, "disk_usage", lambda path: SimpleNamespace(free=free.get(Path(path), p.MIN_FREE_BYTES)))
    docker_root = tmp_path / "docker"
    p.CONTAINERD_ROOT.mkdir(parents=True)
    state = tmp_path / "state"
    free[state] = p.MIN_FREE_BYTES - 1
    with pytest.raises(p.Refused, match="15 GiB"):
        p.check_image_space(docker_root, False, state)  # an unknown store keeps the full floor on the state disk
    free[docker_root] = p.MIN_FREE_BYTES - 1
    p.check_image_space(docker_root, True, state)  # image bytes do not land on this disk
    with pytest.raises(p.Refused, match="15 GiB"):
        p.check_image_space(docker_root)  # an unknown store keeps the full floor on both
    free[docker_root] = p.HOST_FREE_BYTES - 1
    with pytest.raises(p.Refused, match="5 GiB"):
        p.check_image_space(docker_root, True)
    free[docker_root], free[p.CONTAINERD_ROOT] = p.MIN_FREE_BYTES, p.MIN_FREE_BYTES - 1
    with pytest.raises(p.Refused, match="containerd"):
        p.check_image_space(docker_root, True)


# spec: ANYM-439
@pytest.mark.parametrize(("answer", "expected"), [
    ('[["driver-type","io.containerd.snapshotter.v1"]]', True), ('[["Backing Filesystem","extfs"]]', False), ("not json", False)])
def test_the_image_store_is_read_from_docker_and_unknown_is_not_containerd(monkeypatch, answer, expected):
    monkeypatch.setattr(p, "run", lambda argv, **kw: answer)
    assert p.containerd_image_store() is expected


# spec: ANYM-439
def test_a_disk_close_to_its_floor_warns_before_the_refusal(monkeypatch):
    usage = {p.CONTAINERD_ROOT: p.MIN_FREE_BYTES + 1, p.DOCKER_ROOT: p.HOST_FREE_BYTES + 1}
    monkeypatch.setattr(p.shutil, "disk_usage", lambda path: SimpleNamespace(free=usage[Path(path)]))
    assert [line.split(":")[0] for line in p.disk_warnings()] == [str(p.CONTAINERD_ROOT), str(p.DOCKER_ROOT)]
    assert p.cleanup_line(None, None)["disk_warnings"] == p.disk_warnings()
    usage.update({p.CONTAINERD_ROOT: 30 * 1024**3, p.DOCKER_ROOT: 20 * 1024**3})
    assert p.disk_warnings() == [] and "disk_warnings" not in p.cleanup_line(None, None)
    monkeypatch.setattr(p.shutil, "disk_usage", lambda path: (_ for _ in ()).throw(OSError()))
    assert p.disk_warnings() == []


class LifecycleHost:
    """A `Deployer` whose docker is a `RetiredHost`: the real container cleanup, image retention and capacity gate run
    against one fake Docker, and the space the gate sees is credited only when image 1 has actually disappeared."""

    def __init__(self, tmp_path, monkeypatch, images, *, running=False):
        self.host = Deployer(tmp_path, monkeypatch, history=(NEWER,), green=(NEWER,))
        host = self.host
        state, detail = http_generations(host.http_state, images, serving=BASE, digest=p.digest(host.http))
        host.history = ["%040x" % (0xa0 + number) for number in range(len(images) - 1)] + [BASE, NEWER]
        if running:
            for identity in sorted(detail)[:2]:
                detail[identity]["State"] = {"Status": "running", "Running": True}
        # The image pass reads complete plan A receipts of the current release (images and rollback snapshot).
        current = host.current
        def complete(sha):
            current(sha)
            image, before = (image_id("3"), image_id("2")) if sha == BASE else (image_id("4"), image_id("3"))
            release = host.state / sha
            p.write(release / "prepared.json", p.read(release / "prepared.json") | {"images": {"runtime": image}, "projects": [{"name": "synthetic"}]})
            p.write(release / "synthetic.json", {"services": {"api": {"image": image}}})
            p.write(release / "synthetic.before.json", {"services": {"api": {"image": before}, "db": {"image": "postgres"}}})
            p.write(release / "result.json", {"sha": sha, "images": {"runtime": image}, "status": "applied", "applied_at": "2026-10-20T00:00:00Z"})
        host.current = complete
        complete(BASE)
        self.http_state, self.store = state, RetiredHost(monkeypatch, [runtime(seed) for seed in "13"], detail)
        monkeypatch.setattr(p, "retain_images", RETAIN_IMAGES)
        def route(argv, **kwargs):
            return host.run(argv, **kwargs) if argv[0] == "git" or argv[:2] == ["docker", "compose"] else self.store.run(argv, **kwargs)
        monkeypatch.setattr(p, "run", route)
        def gate(sha):
            if image_id("1") in self.store.references:
                raise p.Refused("at least 15 GiB free is required on /var/lib/containerd before obtaining production images")
        host.fail_prepare = gate

    def order(self):
        return ["container" if call[1] == "rm" else "image" for call in self.store.calls
                if call[:2] == ["docker", "rm"] or call[:3] == ["docker", "image", "rm"]]


# spec: ANYM-689
def test_a_deploy_under_the_gate_recovers_through_real_container_and_image_removal(tmp_path, monkeypatch):
    life = LifecycleHost(tmp_path, monkeypatch, [image_id("1"), image_id("3"), image_id("3")])
    outcome = life.host.deploy()
    # The gate only passes once image 1 is gone, and it is gone only after the real cleanup removed its two containers.
    assert outcome["status"] == "deployed", (outcome.get("reason"), outcome.get("preflight"))
    assert life.order() == ["container", "container", "image"]
    assert outcome["cleanup"]["containers_removed"] == 2 and outcome["cleanup"]["images_removed"] == 1
    assert outcome["cleanup"]["generations"] == ["000000000001"] and image_id("1") not in life.store.references


# spec: ANYM-689
def test_a_deploy_whose_pinned_image_stays_stops_with_the_refusal_and_names_the_keep_reason(tmp_path, monkeypatch):
    life = LifecycleHost(tmp_path, monkeypatch, [image_id("1"), image_id("3"), image_id("3")], running=True)
    life.host.deploy()
    receipt = p.read(life.host.state / "deploy.json")
    assert receipt["status"] == "halted" and "15 GiB" in receipt["reason"] and life.order() == []
    assert receipt["preflight"]["images"]["kept_reasons"]["used by container <id>"] >= 1
    assert receipt["cleanup"]["containers_removed"] == 0 and image_id("1") in life.store.references


# spec: ANYM-689
def test_the_retirement_in_a_deploy_removes_the_older_containers_and_the_final_pass_frees_their_image(tmp_path, monkeypatch):
    life = LifecycleHost(tmp_path, monkeypatch, [image_id("1"), image_id("3")])
    host, state = life.host, life.http_state
    life.host.fail_prepare = None
    def promote(settings, sha):
        host.steps.append(("seamless promote", sha))
        receipt = {"sha": sha, "generation": "%012x" % 3, "status": "serving", "retired_roles": ["gateway", "api"],
                   "previous": {**p.read(state / BASE / "http-result.json"), "previous": None}, "config_digest": p.digest(host.http),
                   "containers": {role: {"container": "%064x" % (0x900 + number), "image": image_id("3")} for number, role in enumerate(("api", "gateway"))}}
        p.write(state / sha / "http-result.json", receipt)
        p.write(state / sha / "http-attempted.json", {"status": "serving", "sha": sha})
        p.write(state / "http-current.json", receipt)
        return host.s.retired_status(settings, receipt)
    monkeypatch.setattr(host.s, "promote", promote)
    outcome = host.deploy()
    # Nothing was removable before the promotion; its retirement removed generation 1, and only then the image pass ran.
    assert outcome["status"] == "deployed", (outcome.get("reason"), outcome.get("preflight"))
    assert outcome["preflight"]["containers"]["removed"] == []
    assert life.order() == ["container", "container", "image"], (outcome.get("image_removal"), outcome.get("preflight"), life.store.references)
    assert image_id("1") not in life.store.references
    assert outcome["cleanup"]["containers_removed"] == 2 and outcome["cleanup"]["images_removed"] == 1


# spec: ANYM-439
def test_the_command_line_apply_and_images_remove_remove_old_images_under_the_deploy_lock(tmp_path, monkeypatch, capsys):
    import fcntl
    host = Host(tmp_path, monkeypatch)
    monkeypatch.setattr(p, "config", lambda path: host.settings)
    passes = []
    def remove(settings, *, held=()):
        with (host.state / "deploy.lock").open("a") as probe, pytest.raises(BlockingIOError):
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        passes.append(settings["state_directory"])
        return {"removed": [], "failed": [], "kept": 1}
    monkeypatch.setattr(p, "remove_images", remove)
    host.main = RELEASES[0]
    p.main(["prepare", "--sha", RELEASES[0]])
    capsys.readouterr()
    with pytest.raises(SystemExit):
        p.main(["apply", "--sha", RELEASES[1]])  # refused: not prepared
    assert passes == []
    p.main(["apply", "--sha", RELEASES[0]])
    printed = json.loads(capsys.readouterr().out)
    # Printed next to the result; the receipts stay as apply wrote them.
    assert printed["status"] == "applied" and printed["images"] == {"runtime": host.image(RELEASES[0])}
    assert printed["image_removal"] == {"removed": [], "failed": [], "kept": 1} and passes == [str(host.state)]
    assert "image_removal" not in p.read(host.state / "current.json")
    p.main(["images"])
    assert set(json.loads(capsys.readouterr().out)) == {"keep", "candidates"} and len(passes) == 1
    p.main(["images", "--remove"])
    assert json.loads(capsys.readouterr().out) == {"removed": [], "failed": [], "kept": 1} and len(passes) == 2


# spec: ANYM-439
def test_the_disk_floor_also_applies_to_the_containerd_image_store(tmp_path, monkeypatch):
    checked = []
    free = {}
    monkeypatch.setattr(p.shutil, "disk_usage", lambda path: checked.append(Path(path)) or SimpleNamespace(
        free=free.get(Path(path), p.MIN_FREE_BYTES)))
    docker_root = tmp_path / "docker"
    p.check_image_space(docker_root)  # no containerd directory: only Docker's root
    assert checked == [docker_root]
    p.CONTAINERD_ROOT.mkdir(parents=True)
    p.check_image_space(docker_root)  # exactly at the floor is enough
    assert checked[1:] == [docker_root, p.CONTAINERD_ROOT]
    free[p.CONTAINERD_ROOT] = p.MIN_FREE_BYTES - 1
    with pytest.raises(p.Refused, match=re.escape(f"15 GiB free is required on {p.CONTAINERD_ROOT}") + ".*images --remove"):
        p.check_image_space(docker_root)


# spec: ANYM-439
@pytest.mark.parametrize("preparation", ["prepare", "prepare_router"])
def test_a_full_containerd_image_store_stops_both_preparations_before_any_fetch(tmp_path, monkeypatch, preparation):
    settings, calls, _ = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(p, "check_space", CHECK_SPACE)
    # The real verification: it would run git (recorded in `calls`) and reach GitHub before any disk check.
    monkeypatch.setattr(p, "promotable", PROMOTABLE)
    monkeypatch.setattr(p.urllib.request, "urlopen", lambda *args, **kwargs: pytest.fail("network before the disk check"))
    p.CONTAINERD_ROOT.mkdir(parents=True)
    monkeypatch.setattr(p.shutil, "disk_usage", lambda path: SimpleNamespace(
        free=p.MIN_FREE_BYTES - (Path(path) == p.CONTAINERD_ROOT)))
    with pytest.raises(p.Refused, match="containerd"):
        getattr(p, preparation)(settings, SHA)
    assert calls == [] and list(Path(settings["state_directory"]).iterdir()) == []


CALL = "5c1d6f0e-7a2b-4c3d-9e8f-0a1b2c3d4e5f"
OTHER_CALL = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"


def calls_database(directory, statuses, *, rows=0):
    """A synthetic calls database the way services/calls/core.py writes it (journal mode DELETE)."""
    import sqlite3
    database = sqlite3.connect(directory / "voice.sqlite3")
    database.execute("PRAGMA journal_mode=DELETE")
    database.execute("CREATE TABLE calls (id TEXT PRIMARY KEY, brief_id TEXT UNIQUE NOT NULL, body TEXT NOT NULL)")
    database.executemany("INSERT INTO calls VALUES (?, ?, ?)", [
        (identity, "brief-" + identity, json.dumps({"status": status, "brief": {"goal": "synthetic"}}))
        for identity, status in statuses.items()] + [
        (f"row-{index}", f"brief-row-{index}", json.dumps({"status": "finished"})) for index in range(rows)])
    database.commit()
    return database


@pytest.fixture
def calls_dir(tmp_path):
    directory = tmp_path / "calls"
    directory.mkdir()
    return directory


def calls_probe(directory, limit="0.25", lifetime="1", *, code=0):
    # The probe as its container runs it: `python -I -c`, its own environment, nothing from this process.
    result = subprocess.run([sys.executable, "-I", "-c", p.CALLS_PROBE, limit, lifetime, *p.CALL_STATUSES],
                            env={"VOICE_DATA_DIR": str(directory)}, capture_output=True, text=True, timeout=60)
    assert result.returncode == code, result.stderr
    return json.loads(result.stdout) if code == 0 else result.stdout


# spec: ANYM-442
def test_the_calls_probe_reads_only_active_calls_and_changes_nothing(calls_dir):
    identities = {status: f"{index:08x}-0000-4000-8000-000000000000" for index, status in enumerate(p.CALL_STATUSES)}
    calls_database(calls_dir, {**{identity: status for status, identity in identities.items()},
                              OTHER_CALL: "finished", CALL: "interrupted"}).close()
    before = {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in calls_dir.iterdir()}
    assert calls_probe(calls_dir) == {"calls": sorted([identity, status] for status, identity in identities.items())}
    assert {path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in calls_dir.iterdir()} == before


# spec: ANYM-442
def test_the_calls_probe_reports_a_locked_slow_or_unrecovered_database_by_category(calls_dir):
    assert calls_probe(calls_dir) == {"error": "no_database"}
    database = calls_database(calls_dir, {CALL: "active"}, rows=3000)
    # Past its read limit it stops and releases the shared lock instead of holding the service's writes.
    assert calls_probe(calls_dir, limit="0") == {"error": "interrupted"}
    database.execute("BEGIN EXCLUSIVE")
    assert calls_probe(calls_dir) == {"error": "busy"}
    database.rollback()
    database.close()
    # A writer that died mid-transaction leaves a hot journal; a read-only reader never recovers it.
    crash = ("import os, sqlite3, sys; db = sqlite3.connect(sys.argv[1], isolation_level=None);"
             "db.execute('PRAGMA cache_size=1'); db.execute('BEGIN');"
             "db.execute('UPDATE calls SET body = body || ?', ('y' * 5000,)); os._exit(0)")
    subprocess.run([sys.executable, "-I", "-c", crash, str(calls_dir / "voice.sqlite3")], check=True, timeout=60)
    assert (calls_dir / "voice.sqlite3-journal").exists()
    before = {path.name: path.read_bytes() for path in calls_dir.iterdir()}
    assert calls_probe(calls_dir) == {"error": "read_only_recovery_needed"}
    assert {path.name: path.read_bytes() for path in calls_dir.iterdir()} == before
    empty = calls_dir / "empty"
    empty.mkdir()
    import sqlite3
    sqlite3.connect(empty / "voice.sqlite3").close()
    assert calls_probe(empty) == {"error": "no_calls_table"}


# spec: ANYM-442
def test_the_calls_probe_dies_at_its_lifetime_even_inside_a_sqlite_wait(calls_dir):
    database = calls_database(calls_dir, {CALL: "active"})
    database.execute("BEGIN EXCLUSIVE")
    # SQLite would wait 5 s for the lock; the process ends at 1 s by SIGALRM and releases everything.
    assert calls_probe(calls_dir, limit="5", lifetime="1", code=-14) == ""
    database.rollback()


# spec: ANYM-442
@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the permission bits that stand in for a read-only mount")
@pytest.mark.parametrize("sidecars", [False, True], ids=["wal-without-sidecars", "wal-with-sidecars"])
def test_a_wal_database_on_a_read_only_mount_is_read_or_refused_and_never_written(calls_dir, sidecars):
    # The probe's container mounts the calls state directory read-only; permission bits stand in for that here.
    database = calls_database(calls_dir, {CALL: "active"})
    database.execute("PRAGMA journal_mode=WAL")
    database.execute("UPDATE calls SET body = body")
    database.commit()
    if not sidecars:
        database.close()
    try:
        for item in calls_dir.iterdir():
            item.chmod(0o400)
        calls_dir.chmod(0o500)
        before = {item.name: item.read_bytes() for item in calls_dir.iterdir()}
        assert calls_probe(calls_dir) == ({"calls": [[CALL, "active"]]} if sidecars else
                                         {"error": "read_only_recovery_needed"})
        assert {item.name: item.read_bytes() for item in calls_dir.iterdir()} == before
    finally:
        calls_dir.chmod(0o700)
        if sidecars:
            database.close()


class Clock:
    """time for production.py: a monotonic clock that only `sleep` moves."""

    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class CallHost(Host):
    """Host with the host's second kind of project: `personal-os-voice`, whose `voice` service is the calls target.
    Each project runs its own container; the probe container (`docker create`, then `docker start --attach`) answers from `answers` in turn (the last
    one repeats). Pulls carry each target's own recipe labels. Time is a fake clock."""

    def __init__(self, tmp_path, monkeypatch, answers=((),)):
        super().__init__(tmp_path, monkeypatch)
        self.voice_file = tmp_path / "voice.json"
        self.voice_file.write_text(json.dumps({"name": "personal-os-voice", "services": {
            "voice": {"image": "old", "build": {"context": "/unused"}, "environment": {"VOICE_DATA_DIR": "/data"},
                      "volumes": [{"source": "/existing", "target": "/data"}]}}}))
        self.voice = {"name": "personal-os-voice", "directory": str(tmp_path), "files": [str(self.voice_file)],
                      "services": {"voice": "calls"}}
        self.settings["projects"].append(self.voice)
        self.containers = {"synthetic": self.container, "personal-os-voice": self.voice_container("voice0", OLD_IMAGE)}
        self.answers, self.events = list(answers), []
        self.clock = Clock()
        monkeypatch.setattr(p, "time", self.clock)
        monkeypatch.setattr(p, "promotable", self.promotable)
        monkeypatch.setattr(p, "probe_output", self.probe_output)

    def voice_container(self, identity, image):
        return {"Id": identity, "Image": image, "State": {"Running": True},
                "Config": {"Env": ["VOICE_DATA_DIR=/data", "SYNTHETIC=literal"], "User": "10001:10001"},
                "Mounts": [{"Type": "bind", "Source": str(self.root / "voice-data"), "Destination": "/data"}]}

    def promotable(self, sha):
        self.events.append("promotable")
        return {"sha": sha, "run_id": 12}

    def target_image(self, reference):
        """The CI image a pulled reference names: each target carries its own build recipe labels."""
        target = reference.removeprefix(p.REGISTRY).partition("@")[0]
        identity = self.image(self.main) if target == "runtime" else "sha256:" + hashlib.sha256(
            f"ci {target} {self.main}".encode()).hexdigest()
        return identity, ImageStore.image("c", target, revision=self.main, source=self.main) | {"Id": identity}

    def published(self, target, sha):
        reference = verified_image(target, "", "sha256:" + hashlib.sha256(sha.encode()).hexdigest())["reference"]
        return verified_image(target, self.target_image(reference)[0], reference.partition("@")[2])

    def answer(self):
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer if isinstance(answer, str) else json.dumps({"calls": [list(call) for call in answer]})

    def probe_output(self, argv, until):
        """The probe's `docker start --attach`: a failed or timed out start is what probe_output makes of it."""
        assert argv == ["docker", "start", "--attach", "e" * 64] and 0 < until - self.clock.now <= p.CALL_PROBE
        attempted = sorted(str(path.relative_to(self.state)) for path in self.state.glob("*/*attempted.json"))
        self.events.append(("probe", self.inspected, attempted))
        try:
            return self.answer()
        except p.Refused:
            raise p.CallsUnknown("probe_failed") from None
        except subprocess.TimeoutExpired:
            raise p.CallsUnknown("probe_timeout") from None

    def run(self, argv, **kwargs):
        label = next((item.rpartition("=")[2] for item in argv if item.startswith("label=com.docker.compose.project=")),
                     None)
        if argv[:2] == ["docker", "create"] and p.CALLS_PROBE in argv:
            assert 0 < kwargs.get("timeout") <= p.CALL_PROBE
            return "e" * 64 + "\n"
        if argv[:2] == ["docker", "ps"] and "label=" + p.CALL_LABEL in argv:
            return ""  # A probe the tool gave up on is gone.
        if argv[:2] in (["docker", "pull"], ["docker", "tag"]) or argv[:3] == ["docker", "image", "inspect"] and "@" in argv[-1]:
            self.calls.append(argv)
            identity, image = self.target_image(argv[-2] if argv[1] == "tag" else argv[-1])
            if argv[1] == "pull":
                self.images[identity] = image
            if argv[1] == "tag":
                self.tags[argv[-1]] = identity
            return identity if argv[1] == "image" else ""
        if argv[:2] == ["docker", "compose"]:
            self.calls.append(argv)
            if "config" in argv:
                return json.dumps(json.loads(Path(argv[argv.index("-f") + 1]).read_text())).replace("$$", "$")
            name = argv[argv.index("--project-name") + 1]
            container = self.containers[name]
            if "stop" in argv:
                self.events.append(("stop", name))
                container["State"] = {"Running": False}
            if "up" in argv:
                self.serial += 1
                image = json.loads(Path(argv[argv.index("-f") + 1]).read_text())["services"][argv[-1]]["image"]
                self.containers[name] = container = (
                    self.voice_container(f"voice{self.serial}", image) if name == "personal-os-voice" else
                    {"Id": f"{name}{self.serial}", "Image": image, "State": {"Running": True}})
                if name == "synthetic":
                    self.container = container
            if "ps" in argv:
                return container["Id"] + "\n"
            return ""
        if argv[:2] == ["docker", "ps"] and label:
            self.calls.append(argv)
            container = self.containers[label]
            return container["Id"] + "\n" if "--all" in argv or container["State"].get("Running") else ""
        if argv[:2] == ["docker", "inspect"]:
            self.calls.append(argv)
            [container] = [item for item in self.containers.values() if item["Id"] == argv[2]]
            self.inspected = argv[2]
            return json.dumps([container])
        return super().run(argv, **kwargs)

    def prepared(self, sha):
        self.main = sha
        p.prepare(self.settings, sha)
        self.events.clear()

    def stops(self):
        return [event for event in self.events if event[0] == "stop"]


# spec: ANYM-442
def test_apply_waits_for_an_active_call_and_stops_nothing_before_it_ends(tmp_path, monkeypatch, capsys):
    host = CallHost(tmp_path, monkeypatch, answers=([(CALL, "active")], [(CALL, "active")], ()))
    host.prepared(RELEASES[0])
    assert p.apply(host.settings, RELEASES[0])["status"] == "applied"
    # Two polls while the call runs, the last remote gate, a second check, then the marker and the first stop.
    assert host.events[:7] == ["promotable", ("probe", "voice0", []), ("probe", "voice0", []),
                               ("probe", "voice0", []), "promotable", ("probe", "voice0", []), ("stop", "synthetic")]
    assert host.clock.sleeps == [p.CALL_POLL, p.CALL_POLL]
    # Before the second project's stop it checks again, with the marker written.
    assert host.events[7:] == [("probe", "voice0", [f"{RELEASES[0]}/attempted.json"]), ("stop", "personal-os-voice")]
    assert f"calls check: waiting for a call to end: {CALL} (active)" in capsys.readouterr().err


# spec: ANYM-442
def test_a_call_starting_during_the_last_ci_check_is_waited_for_before_anything_is_written(tmp_path, monkeypatch):
    host = CallHost(tmp_path, monkeypatch, answers=((), [(CALL, "ringing")], ()))
    host.prepared(RELEASES[0])
    p.apply(host.settings, RELEASES[0])
    assert host.events[:6] == ["promotable", ("probe", "voice0", []), "promotable", ("probe", "voice0", []),
                               ("probe", "voice0", []), ("stop", "synthetic")]


# spec: ANYM-442
def test_a_call_that_outlasts_the_bound_refuses_apply_before_any_change(tmp_path, monkeypatch, capsys):
    host = CallHost(tmp_path, monkeypatch, answers=([(CALL, "waiting_for_browser")],))
    host.prepared(RELEASES[0])
    host.calls.clear()
    with pytest.raises(p.CallActive, match=re.escape(f"a call is active: {CALL} (waiting_for_browser)")):
        p.apply(host.settings, RELEASES[0])
    # It waited out the bound, never longer, then stopped nothing and wrote nothing in the release.
    assert host.clock.now == p.CALL_WAIT and host.events.count("promotable") == 1
    assert host.mutations() == [] and not (host.state / RELEASES[0] / "attempted.json").exists()
    assert capsys.readouterr().err.count("calls check: waiting") == 1


# spec: ANYM-442
def test_a_call_starting_before_a_later_project_is_waited_for_and_never_cut(tmp_path, monkeypatch):
    host = CallHost(tmp_path, monkeypatch, answers=((), (), [(CALL, "connecting")]))
    host.prepared(RELEASES[0])
    with pytest.raises(p.CallActive, match=CALL):
        p.apply(host.settings, RELEASES[0])
    # The first project was replaced; the calls project was never stopped. The attempt stays unfinished.
    assert host.stops() == [("stop", "synthetic")] and host.clock.now == p.CALL_WAIT
    assert (host.state / RELEASES[0] / "attempted.json").exists()
    assert not (host.state / RELEASES[0] / "result.json").exists()
    assert p.unfinished(host.state) == [RELEASES[0]]


# spec: ANYM-442
def test_rollback_waits_for_a_call_and_refuses_before_any_change_at_the_bound(tmp_path, monkeypatch):
    host = CallHost(tmp_path, monkeypatch)
    for sha in RELEASES[:2]:
        host.prepared(sha)
        p.apply(host.settings, sha)
    host.events.clear()
    host.answers = [[(CALL, "active")]]
    with pytest.raises(p.CallActive, match=CALL):
        p.rollback(host.settings, RELEASES[1])
    assert host.stops() == [] and not (host.state / RELEASES[1] / "rollback-attempted.json").exists()
    host.events.clear()
    host.answers = [[(CALL, "active")], (), ()]
    assert p.rollback(host.settings, RELEASES[1])["status"] == "rolled-back"
    applied = [f"{sha}/attempted.json" for sha in RELEASES[:2]]
    assert host.events == [("probe", "voice4", applied), ("probe", "voice4", applied), ("stop", "synthetic"),
                           ("probe", "voice4", sorted([*applied, f"{RELEASES[1]}/rollback-attempted.json"])),
                           ("stop", "personal-os-voice")]


# spec: ANYM-442
@pytest.mark.parametrize("answer, category", [
    ('{"error": "busy"}', "busy"),
    ('{"error": "read_only_recovery_needed"}', "read_only_recovery_needed"),
    ('{"error": "/data/voice.sqlite3: no such file"}', "malformed_probe_output"),
    ("Traceback (most recent call last):", "malformed_probe_output"),
    ('{"calls": [["not-a-uuid", "active"]]}', "malformed_probe_output"),
    (f'{{"calls": [["{CALL}", "on_hold"]]}}', "malformed_probe_output"),
    (f'{{"calls": [["{CALL}", "active", "+15550100"]]}}', "malformed_probe_output"),
    ('{"calls": {"brief": "synthetic"}}', "malformed_probe_output"),
    (p.Refused("docker failed (exit 1); production was not accepted"), "probe_failed"),
    (subprocess.TimeoutExpired("docker", 20), "probe_timeout"),
], ids=["busy", "hot-journal", "raw-error", "not-json", "non-uuid", "unknown-status", "extra-field", "not-a-list",
        "exec-failed", "exec-timeout"])
def test_an_unreadable_or_untrusted_probe_answer_is_never_taken_for_no_call(tmp_path, monkeypatch, answer, category):
    host = CallHost(tmp_path, monkeypatch, answers=(answer,))
    host.prepared(RELEASES[0])
    with pytest.raises(p.CallsUnknown) as refused:
        p.apply(host.settings, RELEASES[0])
    # Only the fixed category reaches the refusal, never the probe's own text.
    assert str(refused.value) == "whether a call is active could not be read: " + category
    assert host.mutations() == [] and not (host.state / RELEASES[0] / "attempted.json").exists()


# spec: ANYM-442
def test_calls_are_read_in_a_confined_container_of_every_configured_calls_service(monkeypatch):
    commands = []
    running = {("personal-os-voice", "voice"): "c1", ("second", "calls"): "c2", ("stopped", "voice"): ""}
    def run(argv, **kwargs):
        commands.append((argv, kwargs))
        if argv[:2] == ["docker", "ps"]:
            project, service = (item.rpartition("=")[2] for item in argv if item.startswith("label="))
            return running[project, service] + "\n"
        if argv[:2] == ["docker", "inspect"]:
            mount = ({"Type": "bind", "Source": "/opt/personal-os-voice/data", "Destination": "/data"} if argv[2] == "c1"
                     else {"Type": "volume", "Name": "second_calls", "Destination": "/state"})
            return json.dumps([{"Image": IMAGE, "Mounts": [mount], "Config": {
                "User": "10001:10001", "Env": ["SECRET_KEY=synthetic"] + (["VOICE_DATA_DIR=/state"] if argv[2] == "c2" else [])}}])
        assert argv[:2] == ["docker", "create"]
        return ("b" if "type=bind" in " ".join(argv) else "d") * 64 + "\n"
    def start(argv, until):
        commands.append((argv, {"timeout": until}))
        assert argv[:3] == ["docker", "start", "--attach"]
        return json.dumps({"calls": [[CALL if argv[3] == "b" * 64 else OTHER_CALL, "active"]]})
    monkeypatch.setattr(p, "run", run)
    monkeypatch.setattr(p, "probe_output", start)
    settings = {"projects": [{"name": "personal-os-probe", "services": {"telegram": "runtime"}},
                             {"name": "personal-os-voice", "services": {"voice": "calls"}},
                             {"name": "second", "services": {"calls": "calls", "api": "runtime"}},
                             {"name": "stopped", "services": {"voice": "calls"}}]}
    monkeypatch.setattr(p, "time", Clock())
    assert p.active_calls(settings) == [f"{CALL} (active)", f"{OTHER_CALL} (active)"]
    # Created first, then started by its ID, so the container that may run is known before it starts.
    assert [argv[:2] for argv, _ in commands] == [["docker", "ps"], ["docker", "inspect"], ["docker", "create"],
                                                  ["docker", "start"]] * 2 + [["docker", "ps"]]
    assert commands[3][0] == ["docker", "start", "--attach", "b" * 64]
    assert all(kwargs == {"timeout": p.CALL_PROBE} for _, kwargs in commands)
    probe = commands[2][0]
    image = probe.index(IMAGE)
    assert probe[image + 1:] == ["-I", "-c", p.CALLS_PROBE, str(p.CALL_READ), str(p.CALL_LIFETIME), *p.CALL_STATUSES]
    options = probe[2:image]
    # --init: python as PID 1 would ignore SIGALRM's default action and outlive its lifetime.
    for flag in (["--rm"], ["--init"], ["--pull", "never"], ["--network", "none"], ["--read-only"], ["--cap-drop", "ALL"],
                 ["--memory", "64m"], ["--pids-limit", "16"], ["--user", "10001:10001"],
                 ["--mount", "type=bind,source=/opt/personal-os-voice/data,target=/data,readonly"],
                 ["--env", "VOICE_DATA_DIR=/data"], ["--entrypoint", "python"]):
        assert any(options[i:i + len(flag)] == flag for i in range(len(options))), flag
    # Nothing of the calls container's environment reaches the probe.
    assert not any("SECRET" in item for item in probe)
    assert "type=volume,source=second_calls,target=/data,readonly" in commands[6][0]
    # A configuration without a calls target checks nothing.
    commands.clear()
    assert p.active_calls({"projects": settings["projects"][:1]}) == [] and commands == []


@pytest.fixture(autouse=True)
def no_unconfirmed_probes(monkeypatch):
    monkeypatch.setattr(p, "UNCONFIRMED", set())


VOICE = {"projects": [{"name": "personal-os-voice", "services": {"voice": "calls"}}]}


class ProbeDocker:
    """Docker for one calls container `c1` and the probe containers production.py creates and starts (fake clock).

    Each probe takes the next of `runs`: its output, or `timeout` (the tool gives up while its probe runs), `late`
    (its start reaches Docker `late_by` seconds after the tool gave up; a container removed by then never runs),
    `create-timeout` (the tool gives up on `docker create`, which Docker creates anyway) or `conflict` (the name is
    taken). `stuck` probes survive `docker rm --force`; `foreign` is a running probe of another token; `listing`, when
    set, is what the probe listing prints. Every command takes `step` seconds of the fake clock, at most its timeout,
    after which it times out."""

    def __init__(self, monkeypatch, runs, *, step=0.0, stuck=False, ps_fails=False, late_by=5.0, listing=None):
        self.runs, self.step, self.stuck, self.ps_fails, self.late_by = list(runs), step, stuck, ps_fails, late_by
        self.listing, self.commands, self.started, self.serial = listing, [], [], 0
        # Probe containers: ID -> (token, the time it runs from, None while it is only created).
        self.probes = {"f" * 64: ("f" * 32, 0.0)}
        self.clock = Clock()
        monkeypatch.setattr(p, "time", self.clock)
        monkeypatch.setattr(p, "run", self.run)
        monkeypatch.setattr(p, "probe_output", self.start)

    def running(self):
        return {identity: token for identity, (token, since) in self.probes.items()
                if since is not None and since <= self.clock.now}

    def take(self, argv, timeout):
        self.commands.append((argv, timeout))
        self.started.append(self.clock.now)
        if self.step > timeout:
            self.clock.now += timeout
            raise subprocess.TimeoutExpired(argv, timeout)
        self.clock.now += self.step

    def run(self, argv, **kwargs):
        self.take(argv, kwargs["timeout"])
        if argv[:2] == ["docker", "ps"] and "label=" + p.CALL_LABEL in argv:
            if self.ps_fails:
                raise p.Refused("docker failed (exit 1); production was not accepted")
            if self.listing is not None:
                return self.listing
            return "".join(f"{identity}\t{'running' if identity in self.running() else 'created'}\t{token}\n"
                           for identity, (token, _) in self.probes.items())
        if argv[:2] == ["docker", "ps"]:
            return "c1\n"
        if argv[:2] == ["docker", "inspect"]:
            return json.dumps([{"Image": IMAGE, "Config": {"User": "10001:10001"}, "Mounts": [
                {"Type": "bind", "Source": "/srv/calls", "Destination": "/data"}]}])
        if argv[:2] == ["docker", "create"]:
            token = argv[argv.index("--label") + 1].removeprefix(p.CALL_LABEL + "=")
            if self.runs[0] == "conflict":
                self.runs.pop(0)
                raise p.Refused("docker failed (exit 125); production was not accepted")
            self.serial += 1
            identity = f"{self.serial:064x}"
            self.probes[identity] = (token, None)
            if self.runs[0] == "create-timeout":
                self.runs.pop(0)
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            return identity + "\n"
        if argv[:3] == ["docker", "rm", "--force"]:
            if not self.stuck:
                self.probes.pop(argv[3], None)
            return ""
        raise AssertionError(argv)

    def start(self, argv, until):
        """probe_output of `docker start --attach <ID>`."""
        assert argv[:3] == ["docker", "start", "--attach"]
        if until <= self.clock.now:
            raise p.CallsUnknown("probe_timeout")
        identity, outcome = argv[3], self.runs.pop(0)
        token = self.probes[identity][0]
        try:
            self.take(argv, until - self.clock.now)
        except subprocess.TimeoutExpired:
            outcome = "timeout"
        if outcome in ("timeout", "late"):
            self.probes[identity] = (token, self.clock.now + (self.late_by if outcome == "late" else 0))
            raise p.CallsUnknown("probe_timeout")
        del self.probes[identity]  # It ran to its end, and --rm removed it.
        return outcome

    def named(self, *names):
        return [argv for argv, _ in self.commands if argv[:2] in [["docker", name] for name in names]]


FOREIGN = {"f" * 64: "f" * 32}
FIRST = f"{1:064x}"


# spec: ANYM-442
def test_a_probe_the_client_gave_up_on_is_killed_by_its_label_and_confirmed_gone(monkeypatch):
    docker = ProbeDocker(monkeypatch, ["timeout"])
    with pytest.raises(p.CallsUnknown, match="probe_timeout"):
        p.active_calls(VOICE)
    [probe] = docker.named("create")
    assert "--init" in probe and re.fullmatch(r"deedkit-calls-probe-[0-9a-f]{12}", probe[probe.index("--name") + 1])
    assert docker.named("start") == [["docker", "start", "--attach", FIRST]]
    # Found by its own token in every state, removed by container ID, then listed again: only the other probe is left.
    assert docker.named("rm") == [["docker", "rm", "--force", FIRST]] and docker.running() == FOREIGN
    assert [argv[:2] for argv, _ in docker.commands[4:]] == [["docker", "ps"], ["docker", "rm"], ["docker", "ps"]]
    assert all(timeout == p.CALL_KILL and "--all" in argv and "-q" not in argv for argv, timeout in docker.commands[4:]
               if argv[1] == "ps")


# spec: ANYM-442
@pytest.mark.parametrize("failure", ["survives-kill", "unlistable"])
def test_a_probe_that_cannot_be_confirmed_gone_ends_the_wait_with_no_further_probe(monkeypatch, failure):
    docker = ProbeDocker(monkeypatch, ["timeout", '{"calls": []}'], stuck=failure == "survives-kill",
                         ps_fails=failure == "unlistable")
    with pytest.raises(p.ProbeStuck, match=re.escape(f"inspect `docker ps --all --filter label={p.CALL_LABEL}`")):
        p.wait_for_calls(VOICE, p.CALL_WAIT)
    # Neither a second probe nor any wait: a stuck probe is not a call that may end.
    assert len(docker.named("create")) == 1 and docker.clock.sleeps == []
    assert not issubclass(p.ProbeStuck, p.CallsUnknown) and not issubclass(p.ProbeStuck, p.CallActive)


# spec: ANYM-442
@pytest.mark.parametrize("listing", ["abcdef123456\n", f"{'1' * 64}\trunning\n", "1\trunning\ttoken\n"],
                         ids=["quiet-ids", "no-token", "short-id"])
def test_a_probe_listing_it_cannot_parse_is_never_taken_for_none_left(monkeypatch, listing):
    # `docker ps -q` ignores --format and prints bare IDs: such rows must not read as "no probe of ours runs".
    docker = ProbeDocker(monkeypatch, ["timeout", '{"calls": []}'], listing=listing)
    with pytest.raises(p.ProbeStuck):
        p.wait_for_calls(VOICE, p.CALL_WAIT)
    assert len(docker.named("create")) == 1 and docker.named("rm") == [] and docker.clock.sleeps == []


# spec: ANYM-442
def test_a_probe_name_another_container_holds_is_never_killed(monkeypatch):
    docker = ProbeDocker(monkeypatch, ["conflict"])
    with pytest.raises(p.CallsUnknown, match="probe_failed"):
        p.active_calls(VOICE)
    assert docker.named("rm") == [] and docker.running() == FOREIGN


# spec: ANYM-442
def test_a_probe_that_starts_after_its_client_gave_up_is_killed_before_the_next_probe(monkeypatch):
    # Its start reaches Docker only after the next poll began (Sol r3 N1): its container is removed by ID before
    # that poll, so whenever the start comes, there is nothing left to run.
    docker = ProbeDocker(monkeypatch, ["late", '{"calls": []}'], late_by=11)
    p.wait_for_calls(VOICE, p.CALL_WAIT)
    assert [argv if argv[1] == "rm" else argv[:2] for argv in docker.named("create", "start", "rm")] == [
        ["docker", "create"], ["docker", "start"], ["docker", "rm", "--force", FIRST], ["docker", "create"],
        ["docker", "start"]]
    docker.clock.now = 60
    assert FIRST not in docker.probes and docker.running() == FOREIGN


# spec: ANYM-442
def test_a_probe_whose_creation_the_tool_gave_up_on_is_removed_before_any_start(monkeypatch):
    docker = ProbeDocker(monkeypatch, ["create-timeout", '{"calls": []}'])
    p.wait_for_calls(VOICE, p.CALL_WAIT)
    assert docker.named("rm") == [["docker", "rm", "--force", FIRST]]
    assert docker.named("start") == [["docker", "start", "--attach", f"{2:064x}"]] and docker.running() == FOREIGN


# spec: ANYM-442
@pytest.mark.parametrize("step, timeouts, ended", [(4, [20, 16, 12, 8], 16), (12, [20, 8], 20)], ids=["fits", "spent"])
def test_one_poll_never_runs_a_docker_command_past_its_budget(monkeypatch, step, timeouts, ended):
    docker = ProbeDocker(monkeypatch, ['{"calls": []}'], step=step)
    if step == 4:
        assert p.active_calls(VOICE) == []
    else:
        with pytest.raises(p.CallsUnknown, match="probe_timeout"):
            p.active_calls(VOICE)
    # Commands share the poll's CALL_PROBE seconds, no floor; a probe that never was created needs no cleanup.
    assert [timeout for _, timeout in docker.commands] == timeouts and docker.clock.now == ended
    assert p.UNCONFIRMED == set()


# spec: ANYM-442
def test_no_poll_starts_after_the_deadline_and_the_last_ends_within_its_budget(monkeypatch):
    docker = ProbeDocker(monkeypatch, [], step=12)
    with pytest.raises(p.CallsUnknown, match="could not be read: probe_timeout"):
        p.wait_for_calls(VOICE, p.CALL_WAIT)
    starts = [at for (argv, _), at in zip(docker.commands, docker.started, strict=True)
              if "label=com.docker.compose.service=voice" in argv]
    # Every poll took its whole budget; none started at or after the deadline, and the refusal came at it.
    assert max(starts) < p.CALL_WAIT and docker.clock.now == p.CALL_WAIT
    assert sum(docker.clock.sleeps) <= p.CALL_WAIT and docker.named("create") == []


# spec: ANYM-442
@pytest.mark.parametrize("entry", ["expired", "overslept"])
def test_a_wait_whose_deadline_passed_starts_no_poll(monkeypatch, entry):
    docker = ProbeDocker(monkeypatch, [f'{{"calls": [["{CALL}", "active"]]}}', '{"calls": []}'])
    if entry == "expired":
        # Sol r3 F2: entered after the bound, it refused only after a poll that could still let the stop through.
        docker.clock.now = 960
        with pytest.raises(p.CallsUnknown, match="could not be read: wait_expired$"):
            p.wait_for_calls(VOICE, p.CALL_WAIT)
        assert docker.commands == []
    else:
        sleep = docker.clock.sleep
        docker.clock.sleep = lambda seconds: sleep(seconds + p.CALL_WAIT)  # the host slept far longer than asked
        with pytest.raises(p.CallActive, match=CALL):
            p.wait_for_calls(VOICE, p.CALL_WAIT)
        assert len(docker.named("create")) == 1


# spec: ANYM-442
def test_a_last_ci_check_that_outlasts_the_wait_bound_stops_nothing(tmp_path, monkeypatch):
    host = CallHost(tmp_path, monkeypatch)
    host.prepared(RELEASES[0])
    gate = host.promotable
    def slow(sha):
        result = gate(sha)
        if host.events.count("promotable") == 2:  # the last remote gate, after the first wait
            host.clock.now += 950
        return result
    monkeypatch.setattr(p, "promotable", slow)
    with pytest.raises(p.CallsUnknown, match="could not be read: wait_expired$"):
        p.apply(host.settings, RELEASES[0])
    assert [event[0] for event in host.events if event != "promotable"] == ["probe"]
    assert host.mutations() == [] and not (host.state / RELEASES[0] / "attempted.json").exists()


# spec: ANYM-442
@pytest.mark.parametrize("answer", ['{"calls": [], "brief": "synthetic private text"}', '{"error": {"brief": "x"}}',
                                    '{"error": ["busy"]}', "{}"],
                         ids=["extra-top-level", "error-object", "error-list", "empty"])
def test_only_the_exact_probe_answer_shape_is_taken(tmp_path, monkeypatch, answer):
    host = CallHost(tmp_path, monkeypatch, answers=(answer,))
    host.prepared(RELEASES[0])
    with pytest.raises(p.CallsUnknown) as refused:
        p.apply(host.settings, RELEASES[0])
    assert str(refused.value) == "whether a call is active could not be read: malformed_probe_output"
    assert host.mutations() == [] and not (host.state / RELEASES[0] / "attempted.json").exists()


# spec: ANYM-442
@pytest.mark.parametrize("script, category", [
    ("import sys; sys.stdout.write('x' * 70000)", "malformed_probe_output"),
    ("import sys; sys.stdout.write('{\"calls\": []}' + ' ' * 70000 + 'synthetic private text')",
     "malformed_probe_output"),
    ("import sys; sys.stdout.buffer.write(b'\\xff')", "malformed_probe_output"),
    ("import sys; print('{\"calls\": []}'); sys.exit(3)", "probe_failed"),
    ("import time; print('{\"calls\": []}', flush=True); time.sleep(30)", "probe_timeout"),
], ids=["oversized", "valid-then-oversized", "not-text", "failed", "never-ends"])
def test_probe_output_is_bounded_in_size_and_time_and_a_failed_probe_is_no_answer(script, category):
    import time
    assert p.probe_output([sys.executable, "-c", "print('{\"calls\": []}')"], time.monotonic() + 10) == '{"calls": []}\n'
    started = time.monotonic()
    with pytest.raises(p.CallsUnknown, match=f"^{category}$"):
        p.probe_output([sys.executable, "-c", script], time.monotonic() + 3)
    assert time.monotonic() - started < 6


def waiting_apply(host, monkeypatch, refusals):
    """`deploy`'s apply refuses before any change while `refusals` lasts, as apply does at the bound."""
    step = p.apply
    def apply(settings, sha):
        if refusals:
            host.steps.append(("apply", sha))
            raise refusals.pop(0)
        return step(settings, sha)
    monkeypatch.setattr(p, "apply", apply)


# spec: ANYM-442
def test_deploy_waits_while_a_call_is_active_and_later_applies_the_same_prepared_release(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    waiting_apply(host, monkeypatch, [p.CallActive(f"a call is active: {CALL} (active)")])
    outcome = host.deploy()
    assert outcome["status"] == "waiting" and host.paused() is None
    assert outcome["reason"] == f"a call is active: {CALL} (active); nothing changed, the next run retries"
    assert outcome["call_waits"]["count"] == 1 and p.read(host.state / "deploy.json") == outcome
    assert not (host.state / NEWEST / "attempted.json").exists() and (host.state / NEWEST / "prepared.json").exists()
    outcome = host.deploy()
    assert host.steps == [("prepare", NEWEST), ("apply", NEWEST), ("apply", NEWEST), ("seamless prepare", NEWEST),
                          ("seamless promote", NEWEST)]
    assert outcome["status"] == "deployed" and "call_waits" not in p.read(host.state / "deploy.json")


# spec: ANYM-442
def test_a_newer_attested_commit_replaces_a_release_that_waited_for_a_call(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch, green=(NEWER,))
    waiting_apply(host, monkeypatch, [p.CallActive(f"a call is active: {CALL} (active)")])
    assert host.deploy()["status"] == "waiting"
    host.green.add(NEWEST)
    assert host.deploy()["status"] == "deployed"
    assert host.steps == [("prepare", NEWER), ("apply", NEWER), ("prepare", NEWEST), ("apply", NEWEST),
                          ("seamless prepare", NEWEST), ("seamless promote", NEWEST)]


# spec: ANYM-442
def test_the_fourth_consecutive_wait_for_calls_pauses_and_a_dry_run_keeps_the_count(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    unknown = "whether a call is active could not be read: busy"
    waiting_apply(host, monkeypatch, [p.CallsUnknown(unknown) for _ in range(4)])
    for count in (1, 2, 3):
        outcome = host.deploy()
        assert outcome["status"] == "waiting" and outcome["call_waits"]["count"] == count and host.paused() is None
        assert host.deploy(dry_run=True)["status"] == "plan"
        assert p.read(host.state / "deploy.json")["call_waits"]["count"] == count
    outcome = host.deploy()
    assert outcome["status"] == "halted" and host.paused() == outcome["reason"]
    assert outcome["reason"] == (f"plan A of {NEWEST} waited 4 runs in a row for calls: {unknown}; "
                                 "inspect the calls service")
    assert not (host.state / NEWEST / "attempted.json").exists()


# spec: ANYM-442
def test_a_call_refusal_after_plan_a_started_pauses_as_an_unknown_state(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    def refused(sha):
        raise p.CallActive(f"a call is active: {CALL} (active)")
    host.fail_apply = refused
    outcome = host.deploy()
    assert outcome["status"] == "halted" and "stopped in an unknown state" in outcome["reason"]
    assert (host.state / NEWEST / "attempted.json").exists() and not (host.state / NEWEST / "result.json").exists()
    assert host.paused() == outcome["reason"] and "call_waits" not in outcome


# spec: ANYM-442
@pytest.mark.parametrize("refusal", [p.CallActive(f"a call is active: {CALL} (active)"),
                                     p.CallsUnknown("whether a call is active could not be read: busy")],
                         ids=["call-active", "calls-unknown"])
def test_an_automatic_rollback_that_meets_a_call_pauses_with_no_further_step(tmp_path, monkeypatch, refusal):
    host = Deployer(tmp_path, monkeypatch)
    def unhealthy(sha):
        p.write(host.state / sha / "result.json", {"sha": sha, "status": "unhealthy"})
        raise p.Refused("a replacement failed its health wait")
    def waited(sha):
        raise refusal
    host.fail_apply, host.fail_rollback = unhealthy, waited
    outcome = host.deploy()
    assert host.steps == [("prepare", NEWEST), ("apply", NEWEST), ("rollback", NEWEST)]
    assert outcome["status"] == "halted" and host.paused() == outcome["reason"]
    assert outcome["reason"] == (f"plan A of {NEWEST} failed its health wait and its rollback failed: {refusal}; "
                                 "production state is unknown, inspect it")


# spec: ANYM-442
def test_a_rollback_that_meets_a_call_before_a_later_project_never_stops_it(tmp_path, monkeypatch):
    host = CallHost(tmp_path, monkeypatch)
    for sha in RELEASES[:2]:
        host.prepared(sha)
        p.apply(host.settings, sha)
    host.events.clear()
    host.answers = [(), [(CALL, "active")]]
    with pytest.raises(p.CallActive, match=CALL):
        p.rollback(host.settings, RELEASES[1])
    assert host.stops() == [("stop", "synthetic")] and host.clock.now == p.CALL_WAIT
    assert (host.state / RELEASES[1] / "rollback-attempted.json").exists()
    assert p.unfinished(host.state) == [RELEASES[1]]


# spec: ANYM-442
@pytest.mark.parametrize("between", ["fetch-failed", "ghcr-unreachable"])
def test_a_run_with_another_outcome_between_call_waits_restarts_their_count(tmp_path, monkeypatch, between):
    host = Deployer(tmp_path, monkeypatch)
    waiting_apply(host, monkeypatch, [p.CallActive(f"a call is active: {CALL} (active)") for _ in range(4)])
    for count in (1, 2, 3):
        assert host.deploy()["call_waits"]["count"] == count
    main = p.remote_main
    if between == "fetch-failed":
        def unreadable():
            raise p.Unreachable("main could not be read: git failed (exit 128)")
        monkeypatch.setattr(p, "remote_main", unreadable)
    else:
        host.unreached.add(NEWEST)
    outcome = host.deploy()
    assert outcome["status"] == ("fetch_failed" if between == "fetch-failed" else "error")
    # Astra r1 F4: the failed run copied the count on, and the next wait paused as the fourth in a row.
    assert "call_waits" not in p.read(host.state / "deploy.json")
    monkeypatch.setattr(p, "remote_main", main)
    host.unreached.clear()
    outcome = host.deploy()
    assert outcome["status"] == "waiting" and outcome["call_waits"]["count"] == 1 and host.paused() is None


# spec: ANYM-439
@pytest.mark.parametrize("record", ["rollback-record", "prepared-projects"])
def test_retention_reads_only_the_tool_written_snapshots_of_the_releases_projects(tmp_path, monkeypatch, record):
    settings = retention_settings(tmp_path)
    state = Path(settings["state_directory"])
    released(state, "1" * 40, image_id("1"), image_id("2"))
    release = state / ("1" * 40)
    if record == "rollback-record":
        p.write(state / "current.json", p.read(state / "current.json") | {
            "rollback": {"version": 1, "projects": [{"name": "synthetic"}]}})
    else:
        p.write(release / "prepared.json", p.read(release / "prepared.json") | {"projects": [{"name": "synthetic"}]})
    # An operator's copy of another configuration next to the receipts (production 10-20: runner.before.json made
    # every report fail with KeyError and retention skip itself).
    p.write(release / "runner.before.json", {"runner": {"image": image_id("3")}})
    p.CONFIG_DIRECTORY.mkdir(parents=True)
    Images(monkeypatch, [runtime("1"), runtime("2"), runtime("3")])
    report = p.images_report(settings)
    kept = {item["image"]: item["reasons"] for item in report["keep"]}
    assert kept[image_id("2")] == ["rollback target of the current release"]
    assert [item["image"] for item in report["candidates"]] == [image_id("3")]
    # A named snapshot without its services is no known rollback target: nothing is removed, never a bare KeyError.
    p.write(release / "synthetic.before.json", {"runner": {"image": image_id("2")}})
    assert p.retain_images(settings) == {"skipped": f"the rollback snapshot of {release} is missing or malformed; no image is removed"}


# spec: ANYM-439
@pytest.mark.parametrize("entry", ["image-string", "list", "null"])
def test_a_snapshot_service_entry_that_is_not_a_mapping_removes_nothing(tmp_path, monkeypatch, entry):
    settings = retention_settings(tmp_path)
    state = Path(settings["state_directory"])
    released(state, "1" * 40, image_id("1"), image_id("2"))
    release = state / ("1" * 40)
    unit = {"image-string": image_id("2"), "list": [image_id("2")], "null": None}[entry]
    # Astra r2 F6: such an entry left the rollback image unprotected, and retention removed it.
    p.write(release / "synthetic.before.json", {"services": {"api": unit, "db": {"image": "postgres"}}})
    p.CONFIG_DIRECTORY.mkdir(parents=True)
    store = Images(monkeypatch, [runtime("1"), runtime("2")])
    assert p.retain_images(settings) == {
        "skipped": f"the rollback snapshot of {release} is missing or malformed; no image is removed"}
    assert store.removals() == [] and image_id("2") in store.references


# Deploy tool updates (ANYM-443): a synthetic tool layout under tmp, the fake mirror serving the candidate's tool files,
# and the real self-test run as a separate process with the CI Python against synthetic configurations.
# reads: scripts/production.py
# reads: scripts/seamless_release.py
TOOL = {path: (ROOT / path).read_bytes() for path in p.TOOL_FILES}  # this checkout's tool files
SYNTHETIC_VALUES = ("https://example.invalid/deedkit", "legacy-api:8080", "legacy-gateway:13240", "synthetic-router",
                    "synthetic-frontend", "synthetic-policy-secret")


class Died(BaseException):
    """The deploy process killed at a boundary: no `except Exception` sees it."""


def tree(path):
    """Every entry below `path` with its mode and bytes or link target: what byte-identical compares."""
    found = {}
    for directory, names, files in os.walk(path):
        for name in names + files:
            entry = Path(directory) / name
            info = entry.lstat()
            found[str(entry.relative_to(path))] = (info.st_mode, os.readlink(entry) if stat.S_ISLNK(info.st_mode)
                                                   else entry.read_bytes() if stat.S_ISREG(info.st_mode) else None)
    return found


def candidate_tool(verifier, *, pin=None, inject=b"", seamless=b""):
    """The candidate's production.py (synthetic verifier path and pin, optional code before `main()`) and its
    seamless_release.py (optional code appended)."""
    source = re.sub(rb'(?m)^VERIFIER = Path\(".*"\)$', b'VERIFIER = Path("%s")' % str(verifier).encode(),
                    TOOL["scripts/production.py"])
    source = re.sub(rb'(?m)^VERIFIER_SHA256 = ".*"$', b'VERIFIER_SHA256 = "%s"' % (
        pin or hashlib.sha256(verifier.read_bytes()).hexdigest()).encode(), source)
    marker = b'\nif __name__ == "__main__":\n    main()\n'
    assert source.endswith(marker)
    return {**TOOL, "scripts/production.py": source[:-len(marker)] + inject + marker,
            "scripts/seamless_release.py": TOOL["scripts/seamless_release.py"] + seamless}


def git_blob(content):
    return hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()


def install_release(root, sha, files, installed_at="2026-10-20T00:00:00Z"):
    release = root / "releases" / sha
    release.mkdir(parents=True)
    for path, content in files.items():
        (release / Path(path).name).write_bytes(content)
    (release / "tool.json").write_text(json.dumps({"sha": sha, "blobs": {path: git_blob(content) for path, content
                                                                         in files.items()},
                                                   "installed_at": installed_at}))
    for entry in [release, *release.iterdir()]:
        entry.chmod(0o755 if entry.is_dir() else 0o644)


class ToolHost(Deployer):
    """`Deployer` on a host with the self-updating layout: the bootstrapped tool at BASE is this checkout's pair (the
    running code), the candidate NEWEST changes scripts/production.py."""

    def __init__(self, tmp_path, monkeypatch, *, changes=None, tool_sha=BASE, candidate=NEWEST, **options):
        super().__init__(tmp_path, monkeypatch, changes=changes or {NEWER: ["scripts/production.py"]}, **options)
        self.monkeypatch, self.candidate, self.p = monkeypatch, candidate, p
        self.verifier = fake_verifier(tmp_path, monkeypatch)
        self.tool = tmp_path / "tool"
        self.tool.mkdir(mode=0o755)
        (self.tool / "releases").mkdir(mode=0o755)
        monkeypatch.setattr(p, "TOOL_ROOT", self.tool)
        monkeypatch.setattr(p, "TOOL_PYTHON", sys.executable)
        install_release(self.tool, tool_sha, TOOL, "2026-10-20T01:00:00Z")
        os.symlink(f"releases/{tool_sha}", self.tool / "current")
        for name in ("production.py", "seamless_release.py"):
            os.symlink(f"current/{name}", self.tool / name)
        (tmp_path / "compose.json").write_text("{}")
        router, policy = tmp_path / "router", tmp_path / "policy.json"
        router.mkdir(mode=0o700)
        policy.write_text(json.dumps({"keys": {"agent": "synthetic-policy-secret"}}))
        policy.chmod(0o600)
        self.http = {"version": 1, "state_directory": str(self.http_state), "projects": [
            {"name": "messages", "directory": str(tmp_path), "files": [str(policy)], "services": {"api": "runtime"}},
            {"name": "gateway", "directory": str(tmp_path), "files": [str(policy)], "services": {"gateway": "gateway"}}],
            "http_release": {"api": {"project": "messages", "service": "api"},
                             "gateway": {"project": "gateway", "service": "gateway"}, "gateway_policy": str(policy),
                             "gateway_public_url": "https://example.invalid/deedkit",
                             "router": {"container": "synthetic-router", "image": IMAGE, "network": "synthetic-frontend",
                                        "directory": str(router), "config_file": str(router / "router.json"),
                                        "admin_socket": str(router / "admin.sock"), "listen": 8080},
                             "legacy": {"api": "legacy-api:8080", "gateway": "legacy-gateway:13240"}}}
        p.write(self.http_state / "http-current.json",
                {"sha": BASE, "status": "serving", "config_digest": p.digest(self.http)})
        self.configs = (tmp_path / "plan-a.json", tmp_path / "plan-b.json")
        for path, value in zip(self.configs, (self.settings, self.http)):
            path.write_text(json.dumps(value))
            path.chmod(0o600)
        self.serve(candidate_tool(self.verifier))

    def serve(self, files, sha=None):
        """The fake mirror's tool files at the candidate commit."""
        self.files = files
        for path, content in files.items():
            self.blobs[f"{sha or self.candidate}:{path}"] = git_blob(content)

    def run(self, argv, **kwargs):
        command = argv[3:]
        if command[:2] == ["cat-file", "blob"] and command[2].split(":", 1)[1] in self.files:
            self.git.append(argv)
            assert kwargs.get("binary") is True
            sha, path = command[2].split(":", 1)
            return self.files[path]
        if command[:2] == ["merge-base", "--is-ancestor"] and command[2] not in self.history:
            raise p.Refused("git failed (exit 1); production was not accepted")
        return super().run(argv, **kwargs)

    def deploy(self, **options):
        return self.p.deploy(self.settings, self.http, attested=self.attested, configs=self.configs, **options)

    def restart(self):
        """The next timer run: the production.py `current` names is loaded anew and runs, with the harness's patches
        (not the module's own functions) and exception classes carried over."""
        script = (self.tool / "current" / "production.py").resolve()
        spec = importlib.util.spec_from_file_location(f"installed_tool_{script.parent.name}", script)
        module = importlib.util.module_from_spec(spec)
        # Its tool_module() registers the release's release_enroll.py; this checkout's entry returns at teardown.
        self.monkeypatch.setitem(sys.modules, "release_enroll", sys.modules["release_enroll"])
        spec.loader.exec_module(module)
        for name, value in vars(p).items():
            if isinstance(value, type) and issubclass(value, BaseException) and value.__module__ == p.__name__:
                setattr(module, name, value)
        for target, name, _ in self.monkeypatch._setattr:
            if target is p and getattr(getattr(p, name), "__module__", None) != p.__name__:
                setattr(module, name, getattr(p, name))
        self.monkeypatch.setattr(self.s, "__file__", str(script.parent / "seamless_release.py"))
        self.p = module

    def current_tool(self):
        return os.readlink(self.tool / "current")

    def receipt(self):
        path = self.state / "tool-update.json"
        return p.read(path) if path.exists() else None


# spec: ANYM-443
def test_a_tool_only_range_installs_the_candidates_pair_and_the_next_run_deploys_with_it(tmp_path, monkeypatch):
    host = ToolHost(tmp_path, monkeypatch)
    order, test, write, switch = [], p.self_test_error, host.s.durable_write, p.point_current
    def watched():
        return (tree(host.tool / "releases"), tree(host.state), tree(host.http_state),
                [path.read_bytes() for path in host.configs])
    def self_test(script, configs, sha):
        before = watched()
        error = test(script, configs, sha)
        # The self-test writes nothing: release, configuration and state trees stay byte-identical.
        assert watched() == before and error is None
        order.append("self-test " + ("staging" if ".staging-" in str(script) else str(script.relative_to(host.tool))))
        return error
    monkeypatch.setattr(p, "self_test_error", self_test)
    monkeypatch.setattr(host.s, "durable_write", lambda path, value: (order.append(value["status"]), write(path, value)))
    monkeypatch.setattr(p, "point_current", lambda sha: (order.append("switch " + sha[:1]), switch(sha)))
    outcome = host.deploy()
    assert order == ["self-test staging", "activating", "switch 2", "self-test production.py", "active"]
    assert outcome["status"] == "tool-updated" and outcome["sha"] == NEWEST and outcome["base"] == BASE
    assert outcome["tool_update"] == {"from": BASE, "to": NEWEST, "removed": []}
    assert p.read(host.state / "deploy.json") == outcome and "started_at" not in outcome
    assert host.steps == [] and host.paused() is None and host.current_tool() == f"releases/{NEWEST}"
    release = host.tool / "releases" / NEWEST
    assert {name: (release / name).read_bytes() for name in p.TOOL_FILES.values()} == {
        Path(path).name: content for path, content in host.files.items()}
    assert p.read(release / "tool.json")["blobs"] == {path: git_blob(content) for path, content in host.files.items()}
    assert stat.S_IMODE(release.stat().st_mode) == 0o755 and stat.S_IMODE((release / "tool.json").stat().st_mode) == 0o644
    assert host.receipt()["status"] == "active" and (host.tool / "releases" / BASE).is_dir()
    assert not list((host.tool / "releases").glob("*.staging-*"))
    # The command line ends this run with exit 0; the next timer run is the new tool, and it deploys.
    host.restart()
    outcome = host.deploy()
    assert outcome["status"] == "deployed" and outcome["sha"] == NEWEST
    assert [step for step, sha in host.steps] == ["prepare", "apply", "seamless prepare", "seamless promote"]


def three_file_tool(verifier, manifest=None, extra=b'"""A synthetic third tool module."""\nVALUE = 1\n'):
    """A candidate whose production.py lists one more module, scripts/tool_extra.py, in its TOOL_FILES."""
    files = candidate_tool(verifier)
    manifest = manifest or (b'TOOL_FILES = {"scripts/production.py": "production.py", "scripts/seamless_release.py": '
                            b'"seamless_release.py", "scripts/release_enroll.py": "release_enroll.py", '
                            b'"scripts/tool_extra.py": "tool_extra.py"}')
    source, count = re.subn(rb"(?ms)^TOOL_FILES = \{.*?\}$", manifest.replace(b"\\", b"\\\\"), files["scripts/production.py"])
    assert count == 1
    return {**files, "scripts/production.py": source, "scripts/tool_extra.py": extra}


# spec: ANYM-443
def test_a_candidate_listing_a_third_tool_file_installs_it_deploys_with_it_and_rolls_back_to_the_pair(tmp_path,
                                                                                                    monkeypatch):
    host = ToolHost(tmp_path, monkeypatch)
    host.serve(three_file_tool(host.verifier))
    outcome = host.deploy()
    assert outcome["status"] == "tool-updated" and host.current_tool() == f"releases/{NEWEST}"
    release = host.tool / "releases" / NEWEST
    assert sorted(path.name for path in release.iterdir()) == ["production.py", "release_enroll.py",
                                                               "seamless_release.py", "tool.json", "tool_extra.py"]
    assert p.read(release / "tool.json")["blobs"] == {path: git_blob(content) for path, content in host.files.items()}
    # The release's own list decides its layout, whatever the running tool lists: still an exact set.
    assert p.installed_tool()["sha"] == NEWEST
    (release / "tool_extra.py").rename(tmp_path / "tool_extra.py")
    with pytest.raises(p.Refused, match="must hold exactly"):
        p.tool_release(NEWEST)
    (tmp_path / "tool_extra.py").rename(release / "tool_extra.py")
    (release / "unlisted.py").write_bytes(b"")
    with pytest.raises(p.Refused, match="must hold exactly"):
        p.tool_release(NEWEST)
    (release / "unlisted.py").unlink()
    # The next timer run is the three-file tool: it is the tool `current` names, and it deploys.
    host.restart()
    assert host.p.TOOL_FILES["scripts/tool_extra.py"] == "tool_extra.py" and host.p.stale_tool(host.s) is None
    outcome = host.deploy()
    assert outcome["status"] == "deployed" and outcome["sha"] == NEWEST
    # Its rollback returns to the two-file release, which that release's own list checks.
    assert host.p.tool_rollback(host.settings, host.http, host.s) == {"status": "tool-rolled-back", "from": NEWEST,
                                                                       "to": BASE}
    assert host.current_tool() == f"releases/{BASE}"
    assert host.p.installed_tool()["sha"] == BASE == p.installed_tool()["sha"] and p.stale_tool(host.s) is None


# spec: ANYM-443
def test_a_third_tool_file_that_does_not_compile_fails_the_staged_self_test(tmp_path, monkeypatch):
    host = ToolHost(tmp_path, monkeypatch)
    host.serve(three_file_tool(host.verifier, extra=b"def broken(:\n"))
    before = tree(host.tool)
    outcome = host.deploy()
    assert outcome["status"] == "halted" and f"the self-test of tool {NEWEST} failed" in outcome["reason"]
    assert "SyntaxError" in outcome["reason"] and tree(host.tool) == before and host.receipt() is None


# spec: ANYM-443
def test_a_candidate_that_drops_a_tool_file_the_running_tool_lists_installs_its_own_list(tmp_path, monkeypatch):
    host = ToolHost(tmp_path, monkeypatch)
    host.serve(three_file_tool(host.verifier))
    assert host.deploy()["status"] == "tool-updated"
    host.restart()
    # The next commit removes scripts/tool_extra.py and lists the pair again: git has no blob for the removed path.
    dropped = "3" * 40
    host.history.append(dropped)
    host.green.add(dropped)
    host.changes[dropped], host.candidate = ["scripts/production.py", "scripts/tool_extra.py"], dropped
    host.serve(candidate_tool(host.verifier, inject=b"# the pair again\n"))
    run = host.p.run
    def deleted(argv, **kwargs):
        if argv[3:] == ["rev-parse", f"{dropped}:scripts/tool_extra.py"]:
            raise p.Refused("git failed (exit 128); production was not accepted")
        return run(argv, **kwargs)
    monkeypatch.setattr(host.p, "run", deleted)
    outcome = host.deploy()
    assert outcome["status"] == "tool-updated" and host.current_tool() == f"releases/{dropped}"
    assert sorted(path.name for path in (host.tool / "releases" / dropped).iterdir()) == [
        "production.py", "release_enroll.py", "seamless_release.py", "tool.json"]
    assert p.installed_tool()["sha"] == dropped and host.paused() is None


# spec: ANYM-443
@pytest.mark.parametrize("value", ["[]", "{}", "{1}", "None"])
def test_a_tool_files_value_that_is_no_file_name_is_refused_and_retention_skips_its_release(tmp_path, monkeypatch,
                                                                                            value):
    host = ToolHost(tmp_path, monkeypatch)
    manifest = (b'TOOL_FILES = {"scripts/production.py": "production.py", "scripts/seamless_release.py": '
                b'"seamless_release.py", "scripts/tool_extra.py": %s}' % value.encode())
    files = three_file_tool(host.verifier, manifest)
    with pytest.raises(p.Refused, match="names no TOOL_FILES"):
        p.tool_manifest(files["scripts/production.py"], "synthetic")
    # A release on the host with such a list is no checked release: retention keeps it and removes the others.
    odd = "4" * 40
    install_release(host.tool, odd, {path: content for path, content in files.items() if "extra" not in path})
    install_release(host.tool, "5" * 40, TOOL, "2026-10-19T00:00:00Z")
    assert p.retain_tools([BASE]) == ["5" * 40] and (host.tool / "releases" / odd).is_dir()


# spec: ANYM-443
def test_a_production_py_loaded_by_its_path_runs_with_the_release_enroll_py_beside_it(tmp_path, monkeypatch):
    for path, content in TOOL.items():
        (tmp_path / Path(path).name).write_bytes(content)
    monkeypatch.setitem(sys.modules, "release_enroll", sys.modules["release_enroll"])
    def load():
        # Unregistered, as some readers load it (deedkit admin and calendar_rotate.py register it, others do not).
        spec = importlib.util.spec_from_file_location("production_by_path", tmp_path / "production.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    module, enroll = load(), sys.modules["release_enroll"]
    assert Path(enroll.__file__) == tmp_path / "release_enroll.py" and module.enrollment_pending is enroll.enrollment_pending
    assert enroll.p.Refused is module.Refused and issubclass(module.EnrollRefused, module.Refused)
    module.run = lambda argv, **kwargs: b"patched"  # the loaded module's name, read when the call runs
    assert module.repo_file(tmp_path, "a" * 40, "deploy/modules/x.json") == b"patched"
    (tmp_path / "release_enroll.py").unlink()
    with pytest.raises(ImportError, match="release_enroll.py is missing"):
        load()


# spec: ANYM-443
@pytest.mark.parametrize("manifest", [
    b'TOOL_FILES = dict(production="production.py")',
    b'TOOL_FILES = {"scripts/production.py": "production.py", "scripts/seamless_release.py": "seamless_release.py", '
    b'"tools/tool_extra.py": "tool_extra.py"}',
    b'TOOL_FILES = {"scripts/production.py": "production.py", "scripts/seamless_release.py": "seamless_release.py", '
    b'"scripts/tool_extra.py": "../tool_extra.py"}',
    b'TOOL_FILES = {"scripts/production.py": "production.py", "scripts/tool_extra.py": "tool_extra.py"}',
    b'TOOL_FILES = {"scripts/production.py": "production.py", "scripts/seamless_release.py": "seamless_release.py", '
    b'"scripts/tool.json": "tool.json"}'], ids=["computed", "outside scripts", "path name", "no seamless", "not python"])
def test_a_candidate_whose_tool_files_are_no_literal_list_of_script_modules_pauses_and_installs_nothing(
        tmp_path, monkeypatch, manifest):
    host = ToolHost(tmp_path, monkeypatch)
    host.serve(three_file_tool(host.verifier, manifest))
    before = tree(host.tool)
    outcome = host.deploy()
    assert outcome["status"] == "halted" and outcome["code"] == "tool_by_hand"
    assert f"scripts/production.py of {NEWEST} names no TOOL_FILES" in outcome["reason"]
    assert tree(host.tool) == before and host.receipt() is None and host.paused() == outcome["reason"]
    assert not [argv for argv in host.git if "tool_extra.py" in argv[-1]]  # no file the list names is read


# spec: ANYM-443
@pytest.mark.parametrize("other",["services/messaging/messaging_service/migrations/039_synthetic.sql",
                                   "services/release-router/main.go", "tools/attest-verify/main.go",
                                   "deploy/deedkit-autodeploy.service", "deploy/deedkit-autodeploy.timer"])
def test_a_tool_change_with_an_sql_router_verifier_or_unit_change_pauses_and_installs_nothing(tmp_path, monkeypatch,
                                                                                              other):
    host = ToolHost(tmp_path, monkeypatch, changes={NEWER: ["scripts/production.py"], NEWEST: [other]})
    systemd = tmp_path / "systemd"
    systemd.mkdir()
    for unit in p.UNITS:
        (systemd / Path(unit).name).write_text("[Unit]\nDescription=installed before\n")
    monkeypatch.setattr(p, "SYSTEMD_DIRECTORY", systemd)
    before = tree(host.tool)
    outcome = host.deploy()
    assert outcome["status"] == "halted" and other in outcome["reason"] and "scripts/production.py" in outcome["reason"]
    assert tree(host.tool) == before and host.receipt() is None and host.steps == []


# spec: ANYM-618
@pytest.mark.parametrize("recorded", ["the installed build", "another build"])
def test_a_tool_change_after_a_hand_installed_verifier_updates_itself_only_when_the_candidate_records_that_build(
        tmp_path, monkeypatch, recorded):
    # BASE..NEWEST: NEWER rebuilt the verifier (installed by hand with its tool), NEWEST changes production.py only.
    host = ToolHost(tmp_path, monkeypatch, changes={NEWER: ["tools/attest-verify/main.go", p.VERIFIER_RECORD],
                                                    NEWEST: ["scripts/production.py"]})
    pin = p.VERIFIER_SHA256 if recorded == "the installed build" else "c" * 64
    host.blobs[f"{NEWEST}:{p.VERIFIER_RECORD}"] = git_blob(f"{pin}  attest-verify\n".encode())
    before = tree(host.tool)
    outcome = host.deploy()
    if recorded == "the installed build":
        assert outcome["status"] == "tool-updated" and host.current_tool() == f"releases/{NEWEST}"
        assert host.receipt()["status"] == "active"
    else:
        assert outcome["status"] == "halted" and "tools/attest-verify/main.go" in outcome["reason"]
        assert tree(host.tool) == before and host.receipt() is None and host.steps == []


# spec: ANYM-443
def test_a_changed_autodeploy_unit_pauses_until_the_installed_unit_is_the_candidates(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch, changes={NEWER: ["deploy/deedkit-autodeploy.service"]})
    # A host without the timer installs no unit: nothing to wait for.
    assert host.deploy()["status"] == "deployed"
    (tmp_path / "timer").mkdir()
    host = Deployer(tmp_path / "timer", monkeypatch, changes={NEWER: ["deploy/deedkit-autodeploy.service"]})
    systemd = tmp_path / "systemd"
    systemd.mkdir()
    installed = systemd / "deedkit-autodeploy.service"
    installed.write_text("[Unit]\nDescription=the previous unit\n")
    monkeypatch.setattr(p, "SYSTEMD_DIRECTORY", systemd)
    outcome = host.deploy()
    assert outcome["status"] == "halted" and "deploy/deedkit-autodeploy.service" in outcome["reason"]
    p.PAUSE.unlink()
    host.blobs[f"{NEWEST}:deploy/deedkit-autodeploy.service"] = p.blob(installed)
    assert host.deploy()["status"] == "deployed"


# spec: ANYM-443
def test_a_dry_run_reports_the_tool_update_and_changes_nothing(tmp_path, monkeypatch):
    host = ToolHost(tmp_path, monkeypatch)
    def trees():  # a dry run takes the locks: their files appear
        return tree(host.tool), {name: entry for name, entry in tree(host.state).items() if name != "deploy.lock"}
    before = trees()
    assert host.deploy(dry_run=True) == {"status": "plan", "sha": NEWEST, "base": BASE,
                                         "steps": [f"would update tool {NEWEST}"]}
    assert trees() == before and host.paused() is None


def break_layout(host, case):
    release = host.tool / "releases" / BASE
    if case == "current is a directory":
        (host.tool / "current").unlink()
        (host.tool / "current").mkdir(mode=0o755)
    elif case == "current outside releases":
        (host.tool / "current").unlink()
        os.symlink(str(release), host.tool / "current")
    elif case == "a compatibility path is a file":
        (host.tool / "production.py").unlink()
        (host.tool / "production.py").write_bytes(TOOL["scripts/production.py"])
    elif case == "a symlink inside the release":
        (release / "tool.json").rename(release / "tool.real.json")
        os.symlink("tool.real.json", release / "tool.json")
    elif case == "an extra symlink in releases":
        os.symlink(BASE, host.tool / "releases" / ("9" * 40))
    elif case == "a group-writable file":
        (release / "production.py").chmod(0o664)
    elif case == "a group-writable parent":
        (host.tool / "releases").chmod(0o775)
    elif case == "another owner":
        host.monkeypatch.setattr(p.os, "geteuid", lambda: os.getuid() + 1)
    elif case == "tool.json names other blob ids":
        record = p.read(release / "tool.json")
        record["blobs"]["scripts/seamless_release.py"] = "f" * 40
        (release / "tool.json").write_text(json.dumps(record))


LAYOUT_CASES = ["current is a directory", "current outside releases", "a compatibility path is a file",
                "a symlink inside the release", "an extra symlink in releases", "a group-writable file",
                "a group-writable parent", "another owner", "tool.json names other blob ids"]


# spec: ANYM-443
@pytest.mark.parametrize("case", LAYOUT_CASES)
def test_the_layout_check_refuses_anything_but_the_documented_layout(tmp_path, monkeypatch, case):
    host = ToolHost(tmp_path, monkeypatch)
    assert p.installed_tool()["sha"] == BASE
    break_layout(host, case)
    with pytest.raises(p.Refused):
        p.installed_tool()


# spec: ANYM-443
@pytest.mark.parametrize("case", [case for case in LAYOUT_CASES if case != "another owner"] + ["no layout"])
def test_a_refused_or_missing_layout_or_another_running_tool_keeps_the_hand_install_pause(tmp_path, monkeypatch, case):
    host = ToolHost(tmp_path, monkeypatch)
    if case == "no layout":
        monkeypatch.setattr(p, "TOOL_ROOT", tmp_path / "absent")
    else:
        break_layout(host, case)
    before = tree(host.tool)
    outcome = host.deploy()
    assert outcome["status"] == "halted" and "install them by hand" in outcome["reason"]
    assert ("does not update itself" in outcome["reason"]) is (case != "no layout")
    assert tree(host.tool) == before and host.receipt() is None


# spec: ANYM-443
@pytest.mark.parametrize("case", ["blob bytes", "older commit", "another branch", "verifier pin"])
def test_a_candidate_that_is_not_the_attested_forward_pair_or_needs_another_verifier_pauses(tmp_path, monkeypatch,
                                                                                             case):
    options = {"older commit": {"tool_sha": NEWEST, "candidate": NEWER, "green": (NEWER,)},
               "another branch": {"tool_sha": "9" * 40}}.get(case, {})
    host = ToolHost(tmp_path, monkeypatch, **options)
    if case == "blob bytes":
        host.files = {**host.files, "scripts/production.py": host.files["scripts/production.py"] + b"# tampered\n"}
    if case == "verifier pin":
        host.serve(candidate_tool(host.verifier, pin="c" * 64))
    before = tree(host.tool)
    outcome = host.deploy()
    assert outcome["status"] == "halted" and host.paused() == outcome["reason"]
    assert {"blob bytes": "does not match its git blob id", "older commit": "does not descend from the installed tool",
            "another branch": "does not descend from the installed tool",
            "verifier pin": "install the verifier first"}[case] in outcome["reason"]
    # Nothing is written outside a staging directory, and that is removed.
    assert tree(host.tool) == before and host.receipt() is None and host.steps == []


# spec: ANYM-443
@pytest.mark.parametrize("case", ["syntax", "configuration", "network", "write", "process"])
def test_a_failed_self_test_leaves_the_installed_tool_and_its_files_unchanged(tmp_path, monkeypatch, case):
    host = ToolHost(tmp_path, monkeypatch)
    listener = __import__("socket").create_server(("127.0.0.1", 0))
    written = tmp_path / "written-by-the-self-test"
    inject = {"syntax": b"def broken(:\n",
              "network": b"__import__('socket').socket().connect(('127.0.0.1', %d))\n"
                         % listener.getsockname()[1],
              "write": b"open(%r, 'w').close()\n" % str(written).encode(),
              "process": b"__import__('subprocess').run(['/bin/echo'], capture_output=True)\n"}.get(case, b"")
    seamless = b"\ndef validate_settings(settings):\n    raise Refused('synthetic: this configuration is refused')\n" \
        if case == "configuration" else b""
    host.serve(candidate_tool(host.verifier, inject=inject, seamless=seamless))
    before = tree(host.tool)
    outcome = host.deploy()
    listener.close()
    assert outcome["status"] == "halted" and f"the self-test of tool {NEWEST} failed" in outcome["reason"]
    assert {"syntax": "SyntaxError", "configuration": "synthetic: this configuration is refused",
            "network": "self-test refused socket.connect", "write": "self-test refused open",
            "process": "self-test refused subprocess.Popen"}[case] in outcome["reason"]
    assert tree(host.tool) == before and host.receipt() is None and not written.exists()


# spec: ANYM-443
@pytest.mark.parametrize("receipt", ["current.json", "http-current.json", "deploy.json"])
def test_the_self_test_refuses_a_malformed_receipt(tmp_path, monkeypatch, receipt):
    host = ToolHost(tmp_path, monkeypatch)
    install_release(host.tool, NEWEST, host.files)
    script = host.tool / "releases" / NEWEST / "production.py"
    assert p.self_test_error(script, host.configs, NEWEST) is None
    assert p.self_test_error(script, host.configs, NEWER) is not None  # the ok line names another release
    path = {"current.json": host.state, "http-current.json": host.http_state, "deploy.json": host.state}[receipt]
    path = path / receipt
    path.write_text({"current.json": '{"status": "applied"}', "http-current.json": "not json",
                     "deploy.json": '{"fetch_failures": {"count": -1}}'}[receipt])
    error = p.self_test_error(script, host.configs, NEWEST)
    assert error == f"promotion refused: the receipt {path} is missing or malformed"


# spec: ANYM-443
def test_the_self_test_runs_only_as_the_first_argument_under_its_guard(tmp_path, monkeypatch, capsys):
    host = ToolHost(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        p.main(["self-test", "--config", str(host.configs[0]), "--http-config", str(host.configs[1])])
    assert "self-test must be the first argument" in capsys.readouterr().err
    install_release(host.tool, NEWEST, host.files)
    script = host.tool / "releases" / NEWEST / "production.py"
    result = subprocess.run([sys.executable, "-B", "-E", "-s", str(script), "--config", str(host.configs[0]),
                             "self-test", "--http-config", str(host.configs[1])], capture_output=True, text=True,
                            env={"PATH": "/usr/bin:/bin"}, timeout=60)
    assert result.returncode == 1 and "self-test must be the first argument" in result.stderr


# spec: ANYM-618
def test_a_staged_tool_self_test_accepts_only_the_staged_verifier_its_pin_names(tmp_path, monkeypatch, capsys):
    host = ToolHost(tmp_path, monkeypatch)
    # The installed verifier is the previous build; the staged tool pins the staged build, held under a temporary name.
    staged = tmp_path / "attest-verify.staged"
    staged.write_bytes(b"#!/bin/sh\n# synthetic next build\n")
    staged.chmod(0o755)
    install_release(host.tool, NEWEST, candidate_tool(host.verifier, pin=hashlib.sha256(staged.read_bytes()).hexdigest()))
    script = host.tool / "releases" / NEWEST / "production.py"
    def self_test(*verifier):
        return subprocess.run([sys.executable, "-B", "-E", "-s", str(script), "self-test", "--config",
                               str(host.configs[0]), "--http-config", str(host.configs[1]), *verifier],
                              capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}, cwd="/", timeout=60)
    result = self_test("--verifier", str(staged))
    assert result.returncode == 0 and json.loads(result.stdout) == {"self_test": "ok", "sha": NEWEST}
    # A mismatched pair is refused either way: the staged tool against the installed binary, or against another build.
    result = self_test()
    assert result.returncode == 1 and f"the attestation verifier {host.verifier} is missing" in result.stderr
    assert "install the verifier first" in result.stderr
    other = tmp_path / "attest-verify.other"
    other.write_bytes(b"#!/bin/sh\n# synthetic other build\n")
    other.chmod(0o755)
    result = self_test("--verifier", str(other))
    assert result.returncode == 1 and f"the attestation verifier {other} is missing" in result.stderr
    staged.chmod(0o775)
    result = self_test("--verifier", str(staged))
    assert result.returncode == 1 and f"the attestation verifier {staged} is missing" in result.stderr
    # Only the self-test reads another verifier path: every command that runs the verifier uses the installed one.
    with pytest.raises(SystemExit):
        p.main(["deploy", "--verifier", str(staged)])
    assert "--verifier is only for self-test" in capsys.readouterr().err


BOUNDARIES =["staged", "renamed", "activating", "switched", "before active"]


# spec: ANYM-443
@pytest.mark.parametrize("boundary", BOUNDARIES)
def test_a_deploy_killed_at_any_boundary_of_the_update_ends_in_an_accepted_tool(tmp_path, monkeypatch, boundary):
    host = ToolHost(tmp_path, monkeypatch)
    calls = {"self-test": 0, "write": 0, "switch": 0}
    test, write, switch = p.self_test_error, host.s.durable_write, p.point_current
    death = {"staged": ("self-test", 1), "renamed": ("write", 1), "activating": ("switch", 1),
             "switched": ("self-test", 2), "before active": ("write", 2)}[boundary]
    def counted(name, function):
        def call(*args):
            calls[name] += 1
            if (name, calls[name]) == death:
                raise Died(boundary)
            return function(*args)
        return call
    monkeypatch.setattr(p, "self_test_error", counted("self-test", test))
    monkeypatch.setattr(host.s, "durable_write", counted("write", write))
    monkeypatch.setattr(p, "point_current", counted("switch", switch))
    with pytest.raises(Died):
        host.deploy()
    for module, name, original in ((p, "self_test_error", test), (host.s, "durable_write", write),
                                   (p, "point_current", switch)):
        monkeypatch.setattr(module, name, original)
    # Release steps run only with an accepted tool: never while the receipt is `activating`.
    accepted, prepare = [], p.prepare
    monkeypatch.setattr(p, "prepare", lambda settings, sha: (accepted.append(host.receipt()["status"]),
                                                             prepare(settings, sha)))
    outcomes = []
    for _ in range(3):
        host.restart()
        outcomes.append(host.deploy()["status"])
        if outcomes[-1] == "deployed":
            break
    expected = ["tool-updated", "deployed"] if boundary in ("staged", "renamed", "activating") else ["deployed"]
    assert outcomes == expected and accepted == ["active"] and host.paused() is None
    assert host.current_tool() == f"releases/{NEWEST}" and host.receipt()["status"] == "active"
    assert not list((host.tool / "releases").glob("*.staging-*"))


# spec: ANYM-443
@pytest.mark.parametrize("when", ["in the run", "in the next run"])
def test_a_failed_self_test_after_the_switch_restores_the_previous_tool(tmp_path, monkeypatch, when):
    host = ToolHost(tmp_path, monkeypatch)
    test, calls = p.self_test_error, []
    def failing(script, configs, sha):
        calls.append(script)
        if when == "in the next run" and len(calls) == 2:
            raise Died("after the switch")
        return test(script, configs, sha) if len(calls) == 1 else "synthetic: the switched tool fails"
    monkeypatch.setattr(p, "self_test_error", failing)
    if when == "in the next run":
        with pytest.raises(Died):
            host.deploy()
        assert host.current_tool() == f"releases/{NEWEST}" and host.receipt()["status"] == "activating"
        host.restart()
    outcome = host.deploy()
    assert outcome["status"] == "halted" and "synthetic: the switched tool fails" in outcome["reason"]
    assert f"points at {BASE} again" in outcome["reason"] and host.current_tool() == f"releases/{BASE}"
    assert host.receipt()["status"] == "reverted" and host.steps == []


# spec: ANYM-443
def test_tool_rollback_restores_the_previous_pair_pauses_and_the_rolled_back_pair_is_not_installed_again(
        tmp_path, monkeypatch, capsys):
    host = ToolHost(tmp_path, monkeypatch)
    with pytest.raises(p.Refused, match="no active tool update"):
        p.tool_rollback(host.settings, host.http, host.s)
    assert host.deploy()["status"] == "tool-updated"
    with p.lock(host.state), pytest.raises(p.Busy):
        p.tool_rollback(host.settings, host.http, host.s)
    assert p.tool_rollback(host.settings, host.http, host.s) == {"status": "tool-rolled-back", "from": NEWEST,
                                                                  "to": BASE}
    assert host.current_tool() == f"releases/{BASE}" and host.receipt()["status"] == "rolled-back"
    assert host.paused().startswith(f"tool rolled back from {NEWEST} to {BASE}")
    assert host.deploy()["status"] == "paused"
    p.PAUSE.unlink()
    host.restart()
    outcome = host.deploy()
    assert outcome["status"] == "halted" and f"the tool of {NEWEST} was rolled back" in outcome["reason"]
    assert host.current_tool() == f"releases/{BASE}" and host.steps == []
    # A fixed pair in a later commit installs itself again.
    p.PAUSE.unlink()
    fixed = "3" * 40
    host.history.append(fixed)
    host.green.add(fixed)
    host.changes[fixed], host.candidate = ["scripts/production.py"], fixed
    host.serve(candidate_tool(host.verifier, inject=b"# the fixed tool\n"))
    assert host.deploy()["status"] == "tool-updated" and host.current_tool() == f"releases/{fixed}"


# spec: ANYM-443
def test_after_an_update_exactly_two_previous_releases_are_kept(tmp_path, monkeypatch):
    host = ToolHost(tmp_path, monkeypatch)
    older = {"3" * 40: "2026-10-19T12:00:00Z", "4" * 40: "2026-10-19T00:00:00Z", "5" * 40: "2026-10-17T00:00:00Z"}
    for sha, installed_at in older.items():
        install_release(host.tool, sha, TOOL, installed_at)
    (host.tool / "releases" / ("6" * 40)).mkdir()  # no tool.json: not identified as a release, kept
    # BASE replaced "4" * 40; the activation history, not the install time, names the release before the previous one.
    p.write(host.state / "tool-update.json", {"from": "4" * 40, "to": BASE, "at": "2026-10-20T01:00:00Z",
                                              "status": "active", "accepted": ["4" * 40, "5" * 40]})
    outcome = host.deploy()
    assert outcome["status"] == "tool-updated" and sorted(outcome["tool_update"]["removed"]) == ["3" * 40, "5" * 40]
    assert sorted(path.name for path in (host.tool / "releases").iterdir()) == [BASE, NEWEST, "4" * 40, "6" * 40]


# spec: ANYM-443
def test_retention_keeps_the_accepted_releases_and_not_a_newer_reverted_candidate(tmp_path, monkeypatch):
    host = ToolHost(tmp_path, monkeypatch)
    before, reverted = "4" * 40, "3" * 40
    install_release(host.tool, before, TOOL, "2026-10-19T00:00:00Z")
    install_release(host.tool, reverted, TOOL, "2026-10-20T02:00:00Z")  # staged after BASE, reverted after its switch
    p.write(host.state / "tool-update.json", {"from": BASE, "to": reverted, "at": "2026-10-20T02:00:00Z",
                                              "status": "reverted", "reason": "synthetic", "accepted": [BASE, before]})
    outcome = host.deploy()
    assert outcome["status"] == "tool-updated" and outcome["tool_update"]["removed"] == [reverted]
    assert sorted(path.name for path in (host.tool / "releases").iterdir()) == sorted([BASE, NEWEST, before])
    assert host.receipt()["status"] == "active" and host.receipt()["accepted"] == [BASE, before]


# spec: ANYM-443
@pytest.mark.parametrize("pause", ["kept", "removed"])
def test_a_run_waiting_for_the_locks_while_the_tool_is_rolled_back_deploys_nothing(tmp_path, monkeypatch, pause):
    host = ToolHost(tmp_path, monkeypatch)
    assert host.deploy()["status"] == "tool-updated"
    host.restart()
    take = host.p.host_locks
    def rolled_back_first(settings, http, s):
        # tool-rollback runs between this run's pause check and its locks.
        assert p.tool_rollback(settings, http, s)["status"] == "tool-rolled-back"
        if pause == "removed":
            p.PAUSE.unlink()
        return take(settings, http, s)
    monkeypatch.setattr(host.p, "host_locks", rolled_back_first)
    outcome = host.deploy()
    assert outcome["status"] == ("paused" if pause == "kept" else "error")
    assert pause == "kept" or f"this run is not the tool {host.tool / 'current'} names ({BASE})" in outcome["reason"]
    assert host.steps == [] and host.current_tool() == f"releases/{BASE}"


# spec: ANYM-443
def test_a_reused_release_runs_its_verified_source_and_not_a_stale_bytecode_cache(tmp_path, monkeypatch):
    host = ToolHost(tmp_path, monkeypatch)
    install_release(host.tool, NEWEST, host.files)  # an interrupted update left the candidate's release
    stub = tmp_path / "stub" / "seamless_release.py"
    stub.parent.mkdir()
    stub.write_text("def validate_settings(settings):\n    raise SystemExit('the stale cache ran')\n")
    cache = host.tool / "releases" / NEWEST / "__pycache__"
    cache.mkdir(mode=0o755)
    compiled = cache / f"seamless_release.{sys.implementation.cache_tag}.pyc"
    py_compile.compile(str(stub), cfile=str(compiled), doraise=True,
                       invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
    compiled.chmod(0o644)
    outcome = host.deploy()
    assert outcome["status"] == "tool-updated" and not cache.exists()
    assert host.current_tool() == f"releases/{NEWEST}" and host.receipt()["status"] == "active"


# spec: ANYM-443
def test_the_run_after_an_update_executes_the_installed_candidates_code(tmp_path, monkeypatch):
    host = ToolHost(tmp_path, monkeypatch)
    host.serve(candidate_tool(host.verifier, inject=b"def deploy(*args, **kwargs):\n"
                                                    b"    return {'status': 'the candidate ran'}\n"))
    assert host.deploy()["status"] == "tool-updated"
    host.restart()
    assert host.deploy() == {"status": "the candidate ran"}


# spec: ANYM-443
def test_tool_update_receipts_pause_and_journal_line_hold_no_configuration_value(tmp_path, monkeypatch, capsys):
    host = ToolHost(tmp_path, monkeypatch)
    seamless = b"\ndef validate_settings(settings):\n    raise Refused('synthetic refusal')\n"
    host.serve(candidate_tool(host.verifier, seamless=seamless))
    failed = host.deploy()
    p.PAUSE.unlink()
    host.serve(candidate_tool(host.verifier))
    updated = host.deploy()
    install_release(host.tool, NEWER, host.files)
    output = subprocess.run([sys.executable, "-B", "-E", "-s", str(host.tool / "releases" / NEWER / "production.py"),
                             "self-test", "--config", str(host.configs[0]), "--http-config", str(host.configs[1])],
                            capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}, timeout=60)
    texts = [json.dumps(failed), json.dumps(updated), output.stdout + output.stderr,
             (host.state / "tool-update.json").read_text(), (host.state / "deploy.json").read_text()]
    assert failed["status"] == "halted" and updated["status"] == "tool-updated"
    assert not [value for value in SYNTHETIC_VALUES for text in texts if value in text]


# spec: ANYM-443
@pytest.mark.parametrize("path", ["scripts/production.py", "scripts/seamless_release.py"])
def test_both_tool_files_parse_and_compile_as_python_3_12_without_warnings(path):
    import ast
    import warnings
    # reads: scripts/production.py
    # reads: scripts/seamless_release.py
    source = (ROOT / path).read_text()
    ast.parse(source, filename=path, feature_version=(3, 12))
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # an invalid escape (SyntaxWarning) would make the self-test print to stderr
        compile(source, path, "exec", dont_inherit=True)


# spec: ANYM-443
@pytest.mark.parametrize("status", ["malformed", "elsewhere"])
def test_an_unresolved_or_malformed_tool_update_pauses_before_any_other_step(tmp_path, monkeypatch, status):
    host = ToolHost(tmp_path, monkeypatch)
    receipt = host.state / "tool-update.json"
    if status == "malformed":
        receipt.write_text('{"status": "activating"}')
    else:
        install_release(host.tool, NEWER, TOOL)
        (host.tool / "current").unlink()
        os.symlink(f"releases/{NEWER}", host.tool / "current")
        p.write(receipt, {"from": BASE, "to": NEWEST, "at": "2026-10-21T00:00:00Z", "status": "activating"})
    assert host.deploy(dry_run=True)["status"] == "plan" and host.paused() is None
    outcome = host.deploy()
    assert outcome["status"] == "halted" and host.git == [] and host.steps == []
    assert ("malformed" in outcome["reason"]) if status == "malformed" else (
        f"python3 {host.tool}/releases/{BASE}/production.py" in outcome["reason"])


# spec: ANYM-443
def test_status_reads_the_configuration_the_autodeploy_unit_reads(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("DEEDKIT_DEPLOY_CONFIG", raising=False)
    monkeypatch.setattr(p, "CONFIG_DIRECTORY", tmp_path / "etc")
    monkeypatch.setattr(p, "PAUSE", tmp_path / "etc" / "autodeploy.pause")
    monkeypatch.setattr(p, "lock", lambda state: contextlib.nullcontext())
    states = {name: tmp_path / name for name in ("production", "stale", "active", "explicit")}
    for name, state in states.items():
        state.mkdir()
        p.write(state / "current.json", {"sha": name[0] * 40, "status": "applied"})
    monkeypatch.setattr(p, "config", lambda path: {"state_directory": str(states[Path(path).stem])})
    def status(*argv):
        p.main(["status", *argv])
        return json.loads(capsys.readouterr().out)
    # No autodeploy.env: the unit's default.
    assert status()["sha"] == "p" * 40
    p.CONFIG_DIRECTORY.mkdir()
    # As systemd reads it: comments skipped, the last assignment wins.
    (p.CONFIG_DIRECTORY / "autodeploy.env").write_text("DEEDKIT_DEPLOY_CONFIG=/etc/deedkit/stale.json\n"
        "# DEEDKIT_DEPLOY_CONFIG=/etc/deedkit/explicit.json\nDEEDKIT_DEPLOY_CONFIG='/etc/deedkit/active.json'\n")
    printed = status()
    assert printed["sha"] == "a" * 40 and printed["config"] == "/etc/deedkit/active.json"
    # The process environment wins, as systemd's Environment= would for the operator's shell; --config wins over both.
    monkeypatch.setenv("DEEDKIT_DEPLOY_CONFIG", "/etc/deedkit/stale.json")
    assert status()["sha"] == "s" * 40
    assert status("--config", "/etc/deedkit/explicit.json")["sha"] == "e" * 40
    # Other actions keep the fixed default.
    monkeypatch.delenv("DEEDKIT_DEPLOY_CONFIG")
    seen = []
    monkeypatch.setattr(p, "config", lambda path: seen.append(path) or {"state_directory": str(states["stale"])})
    monkeypatch.setattr(p, "images_report", lambda settings: {})
    p.main(["images"])
    assert seen == [p.CONFIG]


# spec: ANYM-525
def test_status_lists_every_configured_service_and_marks_a_lagging_or_stopped_one(tmp_path, monkeypatch, capsys):
    host = WorkersHost(tmp_path, monkeypatch)
    host.release(RELEASES[0])
    monkeypatch.setattr(p, "PAUSE", tmp_path / "autodeploy.pause")
    monkeypatch.setattr(p, "lock", lambda state: contextlib.nullcontext())
    monkeypatch.setattr(p, "config", lambda path: host.settings)
    def status():
        p.main(["status", "--config", "/etc/deedkit/synthetic.json"])
        return json.loads(capsys.readouterr().out)
    def listed():
        return {item["service"]: item for item in status()["project_services"]}
    found = status()
    assert found["sha"] == RELEASES[0]
    assert found["project_services"] == [
        {"project": "deedkit-file-lifecycle", "service": service, "state": "running", "revision": RELEASES[0]}
        for service in LIFECYCLE_SERVICES]
    # A worker left on another revision, one that stopped and one with no container at all.
    host.containers["cold-archive"]["Config"]["Labels"]["org.opencontainers.image.revision"] = RELEASES[1]
    host.containers["cold-restore"]["State"] = {"Running": False, "Status": "exited"}
    del host.containers["file-lifecycle"]
    items = listed()
    assert items["cold-archive"]["mark"] == "differs from current.json" and items["cold-archive"]["revision"] == RELEASES[1]
    assert items["cold-restore"]["mark"] == "not running" and items["cold-restore"]["state"] == "exited"
    assert items["file-lifecycle"]["mark"] == "not running" and items["file-lifecycle"]["revision"] is None
    # After a rollback current.json names the release that does not run: only a stopped service is marked.
    current = p.read(host.state / "current.json")
    p.write(host.state / "current.json", {**current, "status": "rolled-back"})
    items = listed()
    assert "mark" not in items["cold-archive"] and items["cold-restore"]["mark"] == "not running"


# spec: ANYM-525
def test_status_keeps_its_document_when_a_service_cannot_be_read(tmp_path, monkeypatch, capsys):
    host = WorkersHost(tmp_path, monkeypatch)
    host.release(RELEASES[0])
    monkeypatch.setattr(p, "PAUSE", tmp_path / "autodeploy.pause")
    monkeypatch.setattr(p, "lock", lambda state: contextlib.nullcontext())
    monkeypatch.setattr(p, "config", lambda path: host.settings)
    real = host.run
    def run(argv, **kwargs):
        if "label=com.docker.compose.service=cold-archive" in argv:
            raise subprocess.TimeoutExpired(argv, 30)
        return real(argv, **kwargs)
    monkeypatch.setattr(p, "run", run)
    p.main(["status", "--config", "/etc/deedkit/synthetic.json"])
    found = json.loads(capsys.readouterr().out)
    items = {item["service"]: item for item in found["project_services"]}
    assert found["sha"] == RELEASES[0] and items["cold-archive"]["mark"] == "unknown"
    assert items["cold-archive"]["error"] == "TimeoutExpired" and "mark" not in items["cold-restore"]


# spec: ANYM-525
def test_status_bounds_all_its_docker_reads_by_one_budget(tmp_path, monkeypatch):
    host = WorkersHost(tmp_path, monkeypatch)
    host.release(RELEASES[0])
    current = p.read(host.state / "current.json")
    timeouts, clock = [], [1000.0]
    real = host.run
    def run(argv, **kwargs):
        timeouts.append(kwargs["timeout"])
        clock[0] += 25  # A slow daemon: each read answers just inside its own limit.
        return real(argv, **kwargs)
    monkeypatch.setattr(p, "run", run)
    monkeypatch.setattr(p.time, "monotonic", lambda: clock[0])
    items = {item["service"]: item for item in p.project_services(host.settings, current)}
    # Each read gets what is left of the budget, and none starts once it is gone.
    assert timeouts == [30, 30, 10]
    assert items["file-lifecycle"]["revision"] == RELEASES[0] and "mark" not in items["file-lifecycle"]
    assert items["cold-archive"]["mark"] == "unknown" and items["cold-restore"]["error"] == "TimeoutError"


# spec: ANYM-757
def test_status_names_a_stopped_failed_candidate(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(p, "PAUSE", tmp_path / "autodeploy.pause")
    monkeypatch.setattr(p, "lock", lambda state: contextlib.nullcontext())
    state, http_state = tmp_path / "state", tmp_path / "http-state"
    state.mkdir()
    p.write(state / "current.json", {"sha": "c" * 40, "status": "applied"})
    for sha, attempt in (("a" * 40, {"status": "startup-failed", "candidate_stopped": True}),
                         ("b" * 40, {"status": "startup-failed"}), ("d" * 40, {"status": "serving"}),
                         ("e" * 40, {"status": "startup-failed", "candidate_stopped": False})):
        (http_state / sha).mkdir(parents=True)
        p.write(http_state / sha / "http-attempted.json", {**attempt, "sha": sha})
    (tmp_path / "http.json").write_text("{}")
    configs = {"plan-a.json": {"state_directory": str(state)}, "http.json": {"state_directory": str(http_state)}}
    monkeypatch.setattr(p, "config", lambda path: configs[Path(path).name])
    def status(*argv):
        p.main(["status", "--config", str(tmp_path / "plan-a.json"), *argv])
        return json.loads(capsys.readouterr().out)
    printed = status("--http-config", str(tmp_path / "http.json"))
    assert printed["sha"] == "c" * 40
    assert printed["http_failed_attempts"] == [{"sha": "a" * 40, "candidate_stopped": True}, {"sha": "b" * 40},
                                               {"sha": "e" * 40, "candidate_stopped": False}]
    # No plan B on the host: nothing to name, and status never fails because of it.
    assert "http_failed_attempts" not in status("--http-config", str(tmp_path / "absent.json"))
    configs["http.json"] = {}
    assert status("--http-config", str(tmp_path / "http.json"))["http_failed_attempts"] == {"error": "KeyError"}


def freshness_file(path, *, checked_ago=timedelta(minutes=20), status="fresh", age=12.3, **extra):
    checked = datetime.now(timezone.utc) - checked_ago
    p.write(path, {"checked_at": checked.strftime("%Y-%m-%dT%H:%M:%SZ"), "last_snapshot_at": "2026-11-01T16:00:27Z",
                   "max_age_hours": 29, "status": status, "age_hours": age, "exit_code": {"fresh": 0, "stale": 4}.get(status, 5),
                   **extra})
    return checked.strftime("%Y-%m-%dT%H:%M:%SZ")


INFERENCE_WORKER_LINE = "inference worker: no receipt"


def status_with_freshness(tmp_path, monkeypatch, capsys, freshness):
    monkeypatch.delenv("DEEDKIT_DEPLOY_CONFIG", raising=False)
    monkeypatch.setattr(p, "CONFIG_DIRECTORY", tmp_path / "etc")
    monkeypatch.setattr(p, "PAUSE", tmp_path / "etc" / "autodeploy.pause")
    monkeypatch.setattr(p, "lock", lambda state: contextlib.nullcontext())
    monkeypatch.setattr(p, "CALENDAR_FRESHNESS", freshness)
    monkeypatch.setattr(p, "backup_health_lines", lambda: [])  # its own tests below (ANYM-807)
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    p.write(state / "current.json", {"sha": "c" * 40, "status": "applied"})
    monkeypatch.setattr(p, "config", lambda path: {"state_directory": str(state)})
    p.main(["status"])
    printed = capsys.readouterr()
    return SimpleNamespace(out=printed.out, err=printed.err.removesuffix(INFERENCE_WORKER_LINE + "\n"))


def status_with_pause(tmp_path, monkeypatch, capsys, content):
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "autodeploy.pause").write_bytes(content)
    return json.loads(status_with_freshness(tmp_path, monkeypatch, capsys, tmp_path / "freshness.json").out)


def ago(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


# spec: ANYM-766
def test_status_prints_the_halt_with_its_waiting_time_and_keeps_the_existing_keys(tmp_path, monkeypatch, capsys):
    reason = "r" * 1500  # past the 1000 characters of autodeploy_pause: the halt is still read whole
    body = json.dumps({"status": "halted", "reason": reason, "at": ago(hours=3, minutes=7, seconds=10),
                       "code": "configuration_drift", "actor": "agent", "next": "docs/operations/production.md#halts"})
    printed = status_with_pause(tmp_path, monkeypatch, capsys, body.encode() + b"\n")
    assert printed["sha"] == "c" * 40 and printed["autodeploy_pause"] == body[:1000]
    assert printed["autodeploy_halt"] == {"reason": reason, "code": "configuration_drift", "actor": "agent",
                                          "next": "docs/operations/production.md#halts", "waiting": "waiting for 3h7m"}


# spec: ANYM-766
def test_status_keeps_the_text_read_of_autodeploy_pause_for_a_crlf_file(tmp_path, monkeypatch, capsys):
    printed = status_with_pause(tmp_path, monkeypatch, capsys, b"maintenance\r\nwindow\r\n")
    assert printed["autodeploy_pause"] == "maintenance\nwindow" == printed["autodeploy_halt"]["reason"]


# spec: ANYM-766
@pytest.mark.parametrize("content, reason", [
    (json.dumps({"status": "halted", "reason": "old", "at": "2026-11-01T00:00:00Z"}).encode(), "old"),
    (json.dumps({"status": "halted", "reason": "no at", "code": "x", "actor": "agent", "next": "y"}).encode(), "no at"),
    (json.dumps({"reason": "bad at", "at": "yesterday", "code": "x", "actor": "agent", "next": "y"}).encode(), "bad at"),
    (json.dumps({"reason": "number", "at": 5, "code": "x", "actor": "agent", "next": "y"}).encode(), "number"),
    (json.dumps({"reason": "future", "at": "2999-01-01T00:00:00Z", "code": "x", "actor": "agent", "next": "y"}).encode(),
     "future"),
    (b"owner: synthetic maintenance\n", "owner: synthetic maintenance"),
    (b"", ""),  # the documented `touch`
    (b"[1, 2]\n", "[1, 2]"),
    (b"\xff\xfe paused\n", "\ufffd\ufffd paused"),
], ids=["old-shape", "no-at", "bad-at", "number-at", "future-at", "text", "empty", "json-list", "not-utf8"])
def test_status_prints_any_other_pause_file_as_actor_unknown_without_a_waiting_time(tmp_path, monkeypatch, capsys,
                                                                                    content, reason):
    printed = status_with_pause(tmp_path, monkeypatch, capsys, content)["autodeploy_halt"]
    assert printed["reason"] == reason and printed["actor"] == "unknown" and "waiting" not in printed


JSON_VALUES = st.recursive(st.none() | st.booleans() | st.integers() | st.floats() | st.text(),
                           lambda inner: st.lists(inner) | st.dictionaries(st.text(), inner), max_leaves=8)
PAUSE_FILES = st.fixed_dictionaries({}, optional={key: JSON_VALUES | st.sampled_from(["agent", "owner", "2026-11-01T00:00:00Z"])
                                                  for key in ("reason", "at", "code", "actor", "next", "status")})


# spec: ANYM-766
@given(st.text() | JSON_VALUES.map(json.dumps) | PAUSE_FILES.map(json.dumps))
def test_any_pause_text_gives_a_printable_summary_that_claims_a_wait_only_for_a_known_actor(text):
    summary = p.halt_summary(text)
    json.dumps(summary)
    assert summary["actor"] in ("unknown", "agent", "owner")
    assert ("waiting" in summary) == (summary["actor"] != "unknown")
    assert {"reason", "code", "actor", "next"} <= summary.keys()


# spec: ANYM-694
def test_status_prints_the_calendar_backup_freshness_line_beside_the_json_a_reader_parses(tmp_path, monkeypatch, capsys):
    result = tmp_path / "freshness.json"
    checked = freshness_file(result)
    printed = status_with_freshness(tmp_path, monkeypatch, capsys, result)
    assert json.loads(printed.out)["sha"] == "c" * 40
    assert printed.err == f"calendar backup: fresh age=12.3h (checked {checked})\n"
    checked = freshness_file(result, status="stale", age=30.0)
    printed = status_with_freshness(tmp_path, monkeypatch, capsys, result)
    assert printed.err == f"calendar backup: STALE age=30.0h (checked {checked})\n"
    checked = freshness_file(result, status="unknown", age=None, last_snapshot_at=None)
    printed = status_with_freshness(tmp_path, monkeypatch, capsys, result)
    assert printed.err == f"calendar backup: UNKNOWN age=? (checked {checked})\n"


BACKUP_HINT = "backup: verified state is on backup-1 (backup_health.py --print)"


def health_result(path, *, checked_ago=timedelta(minutes=20), profiles=None):
    checked = (datetime.now(timezone.utc) - checked_ago).strftime("%Y-%m-%dT%H:%M:%SZ")
    default = {name: {"word": "delivered", "last_snapshot_at": "2026-11-06T18:00:07Z", "age_hours": 3.2}
               for name in ("messaging", "calendar", "recordings", "calls")}
    p.write(path, {"v": 1, "side": "producer", "checked_at": checked, "profiles": default if profiles is None else profiles})
    return checked


# spec: ANYM-807
@pytest.mark.parametrize("word", ["delivered", "FAILED", "STALE", "UNKNOWN"])
def test_status_prints_one_backup_line_per_profile_with_each_word(tmp_path, word):
    path = tmp_path / "result.json"
    checked = health_result(path, profiles={name: {"word": word, "last_snapshot_at": "2026-11-06T18:00:07Z",
                                                   "age_hours": 3.2} for name in ("messaging", "calendar", "recordings", "calls")})
    assert p.backup_health_lines(path) == [
        *(f"backup {name}: {word} last capture 2026-11-06T18:00:07Z age=3.2h (checked {checked})"
          for name in ("messaging", "calendar", "recordings", "calls")), BACKUP_HINT]


# spec: ANYM-807
def test_a_profile_missing_from_the_result_prints_not_configured(tmp_path):
    path = tmp_path / "result.json"
    health_result(path, profiles={"messaging": {"word": "delivered", "last_snapshot_at": None, "age_hours": None}})
    lines = p.backup_health_lines(path)
    assert lines[0].startswith("backup messaging: delivered last capture never age=?")
    assert lines[1:] == ["backup calendar: not configured", "backup recordings: not configured",
                         "backup calls: not configured", BACKUP_HINT]


# spec: ANYM-807
@pytest.mark.parametrize("case,reason", [("absent", "no recent check"), ("old", "no recent check"),
                                         ("future", "no recent check"), ("malformed", "result unreadable"),
                                         ("not_object", "result unreadable")])
def test_an_absent_old_future_or_malformed_result_prints_four_unknown_lines(tmp_path, case, reason):
    path = tmp_path / "result.json"
    if case == "old":
        health_result(path, checked_ago=timedelta(hours=3, minutes=1))
    elif case == "future":
        health_result(path, checked_ago=timedelta(minutes=-10))
    elif case == "malformed":
        path.write_text("{not json")
    elif case == "not_object":
        path.write_text("[1]")
    assert p.backup_health_lines(path) == [f"backup {name}: UNKNOWN ({reason})"
                                           for name in ("messaging", "calendar", "recordings", "calls")] + [BACKUP_HINT]


# spec: ANYM-807
@given(st.binary(max_size=200) | JSON_VALUES.map(lambda value: json.dumps(value).encode()))
def test_any_backup_result_gives_five_lines_and_no_error(content):
    with tempfile.NamedTemporaryFile() as handle:
        handle.write(content)
        handle.flush()
        lines = p.backup_health_lines(handle.name)
    assert len(lines) == 5 and lines[-1] == BACKUP_HINT
    assert all(line.startswith("backup ") and "\n" not in line for line in lines[:4])


# spec: ANYM-807
def test_status_prints_the_backup_lines_on_stderr_and_keeps_one_json_document(tmp_path, monkeypatch, capsys):
    result = tmp_path / "result.json"
    health_result(result)
    monkeypatch.setattr(p, "BACKUP_HEALTH_RESULT", result)
    monkeypatch.delenv("DEEDKIT_DEPLOY_CONFIG", raising=False)
    monkeypatch.setattr(p, "CONFIG_DIRECTORY", tmp_path / "etc")
    monkeypatch.setattr(p, "PAUSE", tmp_path / "etc" / "autodeploy.pause")
    monkeypatch.setattr(p, "lock", lambda state: contextlib.nullcontext())
    monkeypatch.setattr(p, "CALENDAR_FRESHNESS", tmp_path / "none.json")
    state = tmp_path / "state"
    state.mkdir()
    p.write(state / "current.json", {"sha": "c" * 40, "status": "applied"})
    monkeypatch.setattr(p, "config", lambda path: {"state_directory": str(state)})
    p.main(["status"])
    printed = capsys.readouterr()
    assert json.loads(printed.out)["sha"] == "c" * 40
    err = printed.err.splitlines()
    assert err[0] == "calendar backup: no recent freshness check" and err[-1] == INFERENCE_WORKER_LINE
    assert err[1:6] == p.backup_health_lines(result)


# spec: ANYM-794
@pytest.mark.parametrize("outcome,reason,shown", [("started", None, "started"), ("already_running", None, "already_running"),
                                                  ("refused", "operator_hold", "refused operator_hold")])
def test_status_prints_the_inference_worker_receipt_for_each_outcome(tmp_path, outcome, reason, shown):
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps({"checked_at": "2026-11-05T03:25:00Z", "outcome": outcome, "reason": reason}))
    assert p.inference_worker_line(path) == f"inference worker: {shown} (checked 2026-11-05T03:25:00Z)"


# spec: ANYM-794
def test_status_prints_one_json_document_and_the_inference_worker_line_with_no_receipt(tmp_path, monkeypatch, capsys):
    printed = status_with_freshness(tmp_path, monkeypatch, capsys, tmp_path / "none.json")
    assert json.loads(printed.out)["sha"] == "c" * 40
    assert p.inference_worker_line() == INFERENCE_WORKER_LINE


# spec: ANYM-908
def test_status_shows_the_negotiation_worker_heartbeat_and_owner_zone_only_once_it_is_selected(monkeypatch):
    project = {"name": "deedkit", "directory": "/srv/deedkit/deploy", "files": ["compose.yaml"], "env_files": [],
               "services": {"api": "runtime", "negotiations": "runtime"}}
    assert p.negotiation_worker_line({"projects": [{**project, "services": {"api": "runtime"}}]}) is None
    assert p.negotiation_worker_line({}) is None
    calls = []
    def reply(output):
        def run(argv, **options):
            calls.append(argv)
            return output
        return run
    monkeypatch.setattr(p, "run", reply(json.dumps({"owner_zone": "Europe/Moscow", "heartbeat": {
        "version": "0.9.19+source-filters.source-exclusions", "beat_at": "2026-11-09T12:00:00+00:00"}})))
    assert p.negotiation_worker_line({"projects": [project]}) == ("negotiation worker: heartbeat "
        "0.9.19+source-filters.source-exclusions at 2026-11-09T12:00:00+00:00, owner zone Europe/Moscow")
    assert calls[-1][:2] == ["docker", "compose"] and calls[-1][-7:] == [
        "exec", "-T", "negotiations", "python", "-m", "messaging_service.negotiation_worker", "--status"]
    monkeypatch.setattr(p, "run", reply(json.dumps({"owner_zone": "UTC_fallback", "heartbeat": None})))
    assert p.negotiation_worker_line({"projects": [project]}) == "negotiation worker: no heartbeat, owner zone UTC_fallback"
    for output in ("not json", json.dumps({"owner_zone": "a b"}), json.dumps({"owner_zone": "UTC", "heartbeat": {
            "version": "x y", "beat_at": "now"}})):
        monkeypatch.setattr(p, "run", reply(output))
        assert p.negotiation_worker_line({"projects": [project]}).startswith("negotiation worker: unreadable (")
    def refused(argv, **options):
        raise p.Refused("docker failed (exit 1); production was not accepted")
    monkeypatch.setattr(p, "run", refused)
    assert p.negotiation_worker_line({"projects": [project]}) == "negotiation worker: unreadable (Refused)"


# spec: ANYM-794
@given(st.binary(max_size=200) | JSON_VALUES.map(lambda value: json.dumps(value).encode()))
def test_any_inference_worker_receipt_gives_one_line_and_no_error(content):
    with tempfile.NamedTemporaryFile() as handle:
        handle.write(content)
        handle.flush()
        line = p.inference_worker_line(handle.name)
    assert line.startswith("inference worker: ") and "\n" not in line


# spec: ANYM-694
def test_status_says_there_is_no_recent_freshness_check_for_an_absent_or_old_file(tmp_path, monkeypatch, capsys):
    result = tmp_path / "freshness.json"
    line = "calendar backup: no recent freshness check\n"
    assert status_with_freshness(tmp_path, monkeypatch, capsys, result).err == line
    freshness_file(result, checked_ago=timedelta(hours=3, minutes=1))
    assert status_with_freshness(tmp_path, monkeypatch, capsys, result).err == line
    freshness_file(result, checked_ago=timedelta(hours=2, minutes=59))
    assert status_with_freshness(tmp_path, monkeypatch, capsys, result).err.startswith("calendar backup: fresh ")


# spec: ANYM-694
def test_status_names_an_unreadable_freshness_file_and_never_fails_because_of_it(tmp_path, monkeypatch, capsys):
    result = tmp_path / "freshness.json"
    for text, reason in (("{broken", "not JSON"), ("[1]", "not an object"),
                         ('{"checked_at": "yesterday", "status": "fresh"}', "bad checked_at"),
                         (json.dumps({"checked_at": "2026-11-02T10:00:00Z", "status": "great"}), "bad status")):
        result.write_text(text)
        printed = status_with_freshness(tmp_path, monkeypatch, capsys, result)
        assert printed.err == f"calendar backup: freshness file unreadable ({reason})\n", text
        assert json.loads(printed.out)["status"] == "applied"
    result.unlink()
    result.mkdir()
    printed = status_with_freshness(tmp_path, monkeypatch, capsys, result)
    assert printed.err == "calendar backup: freshness file unreadable (IsADirectoryError)\n"
    # A user who cannot traverse the root-only job directory gets a named reason, not a failed `status`.
    if os.geteuid() != 0:
        result.rmdir()
        locked = tmp_path / "locked"
        locked.mkdir()
        freshness_file(locked / "freshness.json")
        locked.chmod(0)
        try:
            printed = status_with_freshness(tmp_path, monkeypatch, capsys, locked / "freshness.json")
        finally:
            locked.chmod(0o700)
        assert printed.err == "calendar backup: freshness file unreadable (PermissionError)\n"
        assert json.loads(printed.out)["status"] == "applied"


# spec: ANYM-694
def test_the_catchup_service_is_one_of_the_backup_units_the_migration_deploy_waits_for():
    assert p.BACKUP_UNITS == ("personal-os-backup.service", "deedkit-calendar-backup.service",
                              "deedkit-calendar-backup-catchup.service")


# spec: ANYM-443
def test_a_missing_configuration_file_is_named_in_the_refusal(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("DEEDKIT_DEPLOY_CONFIG", raising=False)
    missing = tmp_path / "absent.json"
    with pytest.raises(SystemExit) as stopped:
        p.main(["status", "--config", str(missing)])
    assert stopped.value.code == 1
    assert capsys.readouterr().err == f"promotion refused: FileNotFoundError {missing}\n"
    # An autodeploy.env this parser cannot read as systemd does is refused, not guessed.
    monkeypatch.setattr(p, "CONFIG_DIRECTORY", tmp_path)
    for text, refusal in [("DEEDKIT_DEPLOY_CONFIG=/etc/deedkit/a\\\n.json\n", "cannot read as systemd does"),
                          ("DEEDKIT_DEPLOY_CONFIG=/etc/deedkit/a.json\nNOTES='\nDEEDKIT_DEPLOY_CONFIG=/etc/deedkit/b.json\n'\n",
                           "cannot read as systemd does"),
                          ("OTHER=value\\ \nDEEDKIT_DEPLOY_CONFIG=/etc/deedkit/a.json\n", "cannot read as systemd does"),
                          ("DEEDKIT_DEPLOY_CONFIG=/a.json\n# note\fDEEDKIT_DEPLOY_CONFIG=/b.json\n", "character status cannot read as systemd does"),
                          ("DEEDKIT_DEPLOY_CONFIG=/a.json\u00a0\n", "character status cannot read as systemd does"),
                          ("DEEDKIT_DEPLOY_CONFIG=/a.json\n# note\rDEEDKIT_DEPLOY_CONFIG=/b.json\n",
                           "character status cannot read as systemd does"),
                          ("DEEDKIT_DEPLOY_CONFIG=/etc/deedkit/a.json\nDEEDKIT_DEPLOY_CONFIG=\n", "empty DEEDKIT_DEPLOY_CONFIG")]:
        (tmp_path / "autodeploy.env").write_text(text)
        with pytest.raises(SystemExit):
            p.main(["status"])
        assert refusal + "; pass --config" in capsys.readouterr().err
    # Plain lines with spaces around = and paired quotes read as systemd reads them.
    (tmp_path / "autodeploy.env").write_text('; note\nOTHER = "x y"\nDEEDKIT_DEPLOY_CONFIG = "/etc/deedkit/b.json"\n')
    assert p.active_config() == "/etc/deedkit/b.json"


# spec: ANYM-534
def test_config_promotes_the_matcher_and_dispatcher_overlays(tmp_path, monkeypatch):
    settings, _, _ = fixture(tmp_path, monkeypatch)
    project = settings["projects"][0]
    for name in ("compose.subscriptions.yaml", "compose.delivery.yaml"):
        (Path(project["directory"]) / name).write_text("services: {}\n")
        project["files"].append(str(Path(project["directory"]) / name))
    project["services"].update({"subscriptions": "runtime", "delivery": "runtime"})
    filename = tmp_path / "production.json"
    filename.write_text(json.dumps(settings))
    filename.chmod(0o600)
    loaded = p.config(filename)["projects"][0]
    command = p.compose(loaded)
    files = [command[i + 1] for i, part in enumerate(command) if part == "-f"]
    assert files == project["files"] and files[-2:] == [str(tmp_path / "compose.subscriptions.yaml"),
                                                       str(tmp_path / "compose.delivery.yaml")]
    assert {"subscriptions", "delivery"} <= set(loaded["services"])
    # The documented host file list names both overlays (docs/operations/production.md).
    docs = (Path(__file__).resolve().parents[1] / "docs/operations/production.md").read_text()
    assert "compose.subscriptions.yaml" in docs and "compose.delivery.yaml" in docs
    assert '"subscriptions": "runtime"' in docs and '"delivery": "runtime"' in docs


# spec: ANYM-588
def test_the_migration_section_is_outside_the_configuration_digest(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    before = p.settings_digest(host.settings)
    assert before == p.digest(host.settings)  # receipts prepared before the section still match
    host.settings["migration"] = {"databases": {"main": {"container": "db", "owner": "probe", "database": "probe"}}}
    assert p.settings_digest(host.settings) == before
    assert host.deploy()["status"] == "deployed" and host.paused() is None


# spec: ANYM-588
def test_deploy_pauses_before_any_step_when_a_database_copy_cannot_be_confirmed_stopped(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    copy_id = "c" * 12 + "-main-20261028T120000Z"
    directory = p.copies_root(host.state, create=True) / copy_id
    directory.mkdir(mode=0o700)
    p.write_record(directory / "owner.json", {
        "id": copy_id, "container": "deedkit-copy-" + copy_id, "application_name": "deedkit-copy-" + copy_id,
        "source": {"container": "db", "owner": "probe", "database": "probe"}, "resources": []})
    def stuck(copy):
        raise p.CopyStuck(copy["id"])
    monkeypatch.setattr(p, "release_copy", stuck)
    outcome = host.deploy()
    assert outcome["status"] == "halted" and outcome["reason"] == "a database copy could not be stopped: " + copy_id
    assert host.paused() == outcome["reason"] and host.steps == [] and host.git == []


# spec: ANYM-627
def test_backup_runner_steps_never_pair_a_newer_runner_with_an_older_image():
    # A newer runner writes dump_timeout_seconds into worker.json, which an older worker refuses: the install sets
    # the newer image pin before the newer runner, and the undo puts the older runner back before its image pin.
    docs = (ROOT / "docs/operations/production.md").read_text()
    section = " ".join(docs.split("### Backup runner\n", 1)[1].split("\n## ", 1)[0].split())
    install, undo = section.split("Undo,", 1)
    assert install.index("first set the backup image pin") < install.index("then install the runner") \
        < install.index("sudo install") < install.index("before you add any new `runner.json` key")
    assert undo.index("remove from `runner.json` every key") < undo.index("install the `.before-<sha>` copy") \
        < undo.index("put back the image pin")


# spec: ANYM-608
def test_backup_runner_install_reads_the_prepared_source_under_the_configured_state_directory():
    # prepare() checks the candidate out at <state_directory>/<sha>/source; a fixed host path installs nothing real.
    docs = (ROOT / "docs/operations/production.md").read_text()
    script = docs.split("### Backup runner\n", 1)[1].split("\n## ", 1)[0].split("```sh\n", 1)[1]
    assert re.match(r"""sha=\S+; src=["']?([^"';\s]+)""", script)[1] == "<state_directory>/$sha/source"


class WaitClock(Clock):
    """The fake clock, with the real `time` for everything else, and hooks that run after each sleep."""

    def __init__(self):
        super().__init__()
        self.hooks = []

    def sleep(self, seconds):
        super().sleep(seconds)
        for hook in self.hooks:
            hook(self.now)

    def __getattr__(self, name):
        return getattr(REAL_TIME, name)


REAL_TIME = p.time
PLAN_A_ONLY = {"version": 1}  # wait_backup_idle reads nothing from the configuration


class BackupHost:
    """The host side of `wait_backup_idle`: the runner lock and its `.unsettled` marker as real files, a `systemctl`
    that answers from `states` (unit -> ActiveState, or a function of the fake clock, or an exception to raise), probes
    that each cost `probe` seconds, and a clock that only probes and sleeps move. `hold` takes the lock like a running
    backup, until `until` seconds; `free_while_sleeping` records whether the lock was free at each sleep."""

    def __init__(self, tmp_path, monkeypatch, *, probe=0.0):
        self.tmp_path, self.probe, self.probes, self.on_probe = tmp_path, probe, [], []
        self.lock = tmp_path / "backups" / ".runner.lock"
        self.lock.parent.mkdir(mode=0o700, exist_ok=True)
        self.marker = Path(f"{self.lock}.unsettled")
        monkeypatch.setenv("DEEDKIT_BACKUP_LOCK", str(self.lock))
        self.states, self.fd, self.until, self.free_while_sleeping = {}, None, None, []
        self.clock = WaitClock()
        self.clock.hooks.append(self.sleeping)
        self.inner = p.run
        monkeypatch.setattr(p, "time", self.clock)
        monkeypatch.setattr(p, "run", self.run)

    def run(self, argv, **kwargs):
        if argv[:2] != ["systemctl", "show"]:
            return self.inner(argv, **kwargs)
        # The producer pin's form (`show -P ActiveState <unit>`) is the one every harness fake answers.
        if argv[3] == "InactiveExitTimestamp":
            return "Wed 2026-11-04 18:00:01 UTC\n"
        assert argv[2:4] == ["-P", "ActiveState"], argv
        self.clock.now += self.probe
        self.probes.append(argv[-1])
        for hook in self.on_probe:
            hook(len(self.probes))
        state = self.states.get(argv[-1], "inactive")
        state = state(self.clock.now) if callable(state) else state
        if isinstance(state, Exception):
            raise state
        return f"{state}\n"

    def hold(self, until=None):
        self.fd = os.open(self.lock, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.until = until

    def release(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def lock_is_held(self):
        fd = os.open(self.lock, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)
        return False

    def sleeping(self, now):
        if self.fd is not None and self.until is not None and now >= self.until:
            self.release()
        if self.fd is None:
            self.free_while_sleeping.append(not self.lock_is_held())

    def wait(self, seconds=2700):
        return p.wait_backup_idle(PLAN_A_ONLY, self.clock.monotonic() + seconds, single=seconds == 0)


# spec: ANYM-744
def test_the_wait_returns_at_once_holding_the_lock_when_no_backup_runs(tmp_path, monkeypatch):
    host = BackupHost(tmp_path, monkeypatch)
    with host.wait() as waited:
        assert waited == 0 and host.clock.sleeps == [] and host.lock_is_held()
    assert not host.lock_is_held()
    # Every unit and the marker are read before the lock and again under it.
    assert host.probes == [*p.BACKUP_UNITS, *p.BACKUP_UNITS]


# spec: ANYM-744
def test_every_unit_is_read_the_way_the_producer_pin_reads_it(tmp_path, monkeypatch):
    # The harness fakes of other tests answer exactly `systemctl show -P ActiveState <unit>`; another form would read as
    # an unreadable unit there and wait for the whole bound.
    host = BackupHost(tmp_path, monkeypatch)
    seen = []
    inner = p.run
    monkeypatch.setattr(p, "run", lambda argv, **kwargs: seen.append(argv) or inner(argv, **kwargs))
    with host.wait():
        pass
    assert seen == [["systemctl", "show", "-P", "ActiveState", unit] for unit in [*p.BACKUP_UNITS, *p.BACKUP_UNITS]]


# spec: ANYM-744
def test_a_failed_unit_counts_as_idle(tmp_path, monkeypatch):
    host = BackupHost(tmp_path, monkeypatch)
    host.states["personal-os-backup.service"] = "failed"
    with host.wait():
        assert host.clock.sleeps == []


# spec: ANYM-744
def test_the_wait_returns_holding_the_lock_after_a_backup_that_ends_20_minutes_later(tmp_path, monkeypatch):
    host = BackupHost(tmp_path, monkeypatch)
    host.hold(until=20 * 60)
    with host.wait() as waited:
        assert 20 * 60 <= waited < 20 * 60 + 15 and host.lock_is_held() and host.fd is None
    assert set(host.clock.sleeps) == {p.BACKUP_POLL} and host.free_while_sleeping[-1] is True


# spec: ANYM-744
@pytest.mark.parametrize("state", ["deactivating", "activating", "active", "reloading", "unreadable"])
def test_a_unit_that_is_not_inactive_or_failed_makes_the_wait_continue(tmp_path, monkeypatch, state):
    host = BackupHost(tmp_path, monkeypatch)
    answer = p.Refused("systemctl failed") if state == "unreadable" else state
    host.states["deedkit-calendar-backup.service"] = lambda now: answer if now < 60 else "inactive"
    with host.wait() as waited:
        assert waited >= 60
    assert host.clock.sleeps and all(host.free_while_sleeping)


# spec: ANYM-744
def test_the_unsettled_marker_makes_the_wait_continue(tmp_path, monkeypatch):
    host = BackupHost(tmp_path, monkeypatch)
    host.marker.write_text('{"profile":"calendar"}\n')
    host.clock.hooks.append(lambda now: host.marker.unlink(missing_ok=True) if now >= 60 else None)
    with host.wait() as waited:
        assert waited >= 60
    assert all(host.free_while_sleeping)


# spec: ANYM-744
def test_a_marker_that_appears_between_the_first_check_and_the_lock_releases_it_and_polls_again(tmp_path, monkeypatch):
    host = BackupHost(tmp_path, monkeypatch)
    # The first read under the lock is probe number len(BACKUP_UNITS) + 1: the runner created the marker just then.
    host.on_probe.append(lambda count: host.marker.write_text("{}\n") if count == len(p.BACKUP_UNITS) + 1 else None)
    host.clock.hooks.append(lambda now: host.marker.unlink(missing_ok=True) if now >= 30 else None)
    with host.wait() as waited:
        assert waited >= 30
    assert len(host.probes) > 2 * len(p.BACKUP_UNITS) and host.free_while_sleeping and all(host.free_while_sleeping)


# spec: ANYM-744
def test_a_unit_that_changes_between_the_first_read_and_the_lock_releases_it_too(tmp_path, monkeypatch):
    host = BackupHost(tmp_path, monkeypatch)
    started = len(p.BACKUP_UNITS) + 1
    host.states["personal-os-backup.service"] = lambda now: "activating" if len(host.probes) == started else "inactive"
    with host.wait():
        pass
    assert host.clock.sleeps == [p.BACKUP_POLL] and host.free_while_sleeping == [True]


# spec: ANYM-744
def test_a_probe_that_ends_after_the_deadline_refuses_within_one_probe(tmp_path, monkeypatch):
    host = BackupHost(tmp_path, monkeypatch, probe=60)
    host.states["personal-os-backup.service"] = "activating"
    with pytest.raises(p.BackupRunning) as refused:
        with host.wait(100):
            raise AssertionError("proceeded without the lock")
    # One probe, a sleep to the bound, one more probe: past the bound by less than one probe.
    assert 100 <= host.clock.now < 100 + 60 + p.BACKUP_POLL
    assert refused.value.unit == "personal-os-backup.service" and refused.value.waited_seconds == int(host.clock.now)
    assert refused.value.since == "Wed 2026-11-04 18:00:01 UTC" and not host.lock_is_held()


# spec: ANYM-744
@pytest.mark.parametrize("cause", ["lock", "marker"])
def test_the_refusal_names_the_lock_or_the_marker_that_outlasted_the_bound(tmp_path, monkeypatch, cause):
    host = BackupHost(tmp_path, monkeypatch)
    if cause == "lock":
        host.hold()
    else:
        host.marker.write_text("{}\n")
    with pytest.raises(p.BackupRunning) as refused:
        with host.wait(60):
            raise AssertionError("proceeded without the lock")
    assert refused.value.unit == str(host.lock if cause == "lock" else host.marker)
    assert refused.value.waited_seconds == 60 and refused.value.since is None
    assert refused.value.code == "backup_running" and isinstance(refused.value, p.Busy)


# spec: ANYM-744
def test_a_bound_of_zero_keeps_the_single_non_waiting_check(tmp_path, monkeypatch):
    host = BackupHost(tmp_path, monkeypatch)
    host.hold()
    with pytest.raises(p.BackupRunning) as refused:
        with host.wait(0):
            raise AssertionError("proceeded without the lock")
    assert host.clock.sleeps == [] and refused.value.waited_seconds == 0
    host.release()
    with host.wait(0):
        pass


# spec: ANYM-744
def test_slow_probes_cannot_carry_the_wait_past_a_positive_bound(tmp_path, monkeypatch):
    host = BackupHost(tmp_path, monkeypatch, probe=60)  # every unit idle, each probe takes a minute
    with pytest.raises(p.BackupRunning) as refused:
        with host.wait(30):
            raise AssertionError("admitted after the bound")
    # The first probe ended past the bound: nothing else is probed, nothing is held.
    assert host.clock.now == 60 and host.probes == [p.BACKUP_UNITS[0]] and refused.value.waited_seconds == 60
    assert not host.lock_is_held()


# spec: ANYM-744
def test_a_release_is_never_admitted_after_the_bound_even_when_every_probe_read_idle(tmp_path, monkeypatch):
    host = BackupHost(tmp_path, monkeypatch, probe=10)
    # Six idle probes (before the lock and under it) end at 60 s; the bound is 55 s.
    with pytest.raises(p.BackupRunning):
        with host.wait(55):
            raise AssertionError("admitted after the bound")
    assert host.clock.now <= 55 + 10 and not host.lock_is_held()


# spec: ANYM-744
def test_a_held_lock_with_a_zero_bound_refuses_before_any_probe(tmp_path, monkeypatch):
    host = BackupHost(tmp_path, monkeypatch, probe=60)
    host.hold()
    with pytest.raises(p.BackupRunning):
        with host.wait(0):
            raise AssertionError("proceeded without the lock")
    assert host.probes == [] and host.clock.now == 0


# spec: ANYM-744
def test_a_positive_bound_that_passes_while_the_lock_is_opened_is_still_a_bound(tmp_path, monkeypatch):
    host = BackupHost(tmp_path, monkeypatch, probe=10)
    deadline = host.clock.monotonic() + 60
    opened = p.open_backup_lock

    def slow_open():
        host.clock.now += 100  # descheduled between the caller's deadline and the first probe
        return opened()

    monkeypatch.setattr(p, "open_backup_lock", slow_open)
    with pytest.raises(p.BackupRunning):
        with p.wait_backup_idle(PLAN_A_ONLY, deadline):
            raise AssertionError("admitted after the bound")
    assert host.probes == [p.BACKUP_UNITS[0]] and not host.lock_is_held()


# spec: ANYM-744
def test_the_gate_takes_a_zero_bound_as_the_single_check_and_a_positive_one_as_a_deadline(tmp_path, monkeypatch):
    host = BackupHost(tmp_path, monkeypatch, probe=10)
    with p.backup_gate(PLAN_A_ONLY, 0) as waited:
        assert waited == 60
    host.hold()
    with pytest.raises(p.BackupRunning) as refused:
        with p.backup_gate(PLAN_A_ONLY, 30):
            raise AssertionError("proceeded without the lock")
    assert refused.value.waited_seconds == 30


# spec: ANYM-744
def test_an_idle_host_with_a_zero_bound_is_admitted_whatever_the_probes_cost(tmp_path, monkeypatch):
    host = BackupHost(tmp_path, monkeypatch, probe=60)
    with host.wait(0) as waited:
        assert waited == 360 and host.lock_is_held()


# spec: ANYM-744
def test_a_sigterm_during_the_wait_takes_and_writes_nothing(tmp_path):
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    (bin_directory / "systemctl").write_text("#!/bin/sh\necho inactive\n")
    (bin_directory / "systemctl").chmod(0o755)
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o700)
    lock = backups / ".runner.lock"
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    script = (f"import sys; sys.path.insert(0, {str(ROOT)!r}); from scripts import production as p\n"
              "import time\n"
              "with p.wait_backup_idle({'version': 1}, time.monotonic() + 600):\n"
              "    sys.exit(3)\n")
    env = {"PATH": f"{bin_directory}:{os.environ['PATH']}", "DEEDKIT_BACKUP_LOCK": str(lock)}
    child = subprocess.Popen([sys.executable, "-I", "-c", script], env=env, stderr=subprocess.PIPE, text=True)
    try:
        assert "backup check: waiting" in child.stderr.readline()
        child.terminate()
        assert child.wait(timeout=30) == -15
    finally:
        child.stderr.close()
        child.kill()
        os.close(fd)
    assert sorted(path.name for path in backups.iterdir()) == [".runner.lock"]


def plan_a_file(tmp_path, **extra):
    compose = tmp_path / "compose.json"
    compose.write_text("{}")
    path = tmp_path / "plan-a.json"
    path.write_text(json.dumps({"version": 1, "state_directory": str(tmp_path / "state"), "projects": [
        {"name": "synthetic", "directory": str(tmp_path), "files": [str(compose)], "services": {"api": "runtime"}}],
        **extra}))
    path.chmod(0o600)
    return path


# spec: ANYM-744
@pytest.mark.parametrize("value", [0, 1, 2700, 7200])
def test_the_configuration_accepts_a_backup_wait_in_range(tmp_path, value):
    assert p.config(plan_a_file(tmp_path, backup={"wait_seconds": value}))["backup"] == {"wait_seconds": value}


# spec: ANYM-744
@pytest.mark.parametrize("value", [-1, 7201, "10", 1.5, True, None])
def test_the_configuration_refuses_a_backup_wait_out_of_range_naming_key_value_and_range(tmp_path, value):
    with pytest.raises(p.Refused, match=r"backup\.wait_seconds .* 0 to 7200") as refused:
        p.config(plan_a_file(tmp_path, backup={"wait_seconds": value}))
    assert repr(value) in str(refused.value)


# spec: ANYM-744
@pytest.mark.parametrize("section", [[], "x", {"wait": 5}, {"wait_seconds": 5, "other": 1}])
def test_the_configuration_refuses_a_backup_section_that_is_not_the_wait_seconds_object(tmp_path, section):
    with pytest.raises(p.Refused, match=r"backup must be"):
        p.config(plan_a_file(tmp_path, backup=section))


# spec: ANYM-744
def test_the_backup_section_is_outside_the_configuration_digest_and_defaults_to_45_minutes(tmp_path):
    plain = p.config(plan_a_file(tmp_path))
    with_wait = {**plain, "backup": {"wait_seconds": 5}}
    assert p.settings_digest(plain) == p.settings_digest(with_wait) and p.digest(plain) != p.digest(with_wait)
    assert p.backup_wait(plain) == 2700 and p.backup_wait(with_wait) == 5


# spec: ANYM-744
def test_standalone_apply_waits_for_a_running_backup_and_records_the_wait(tmp_path, monkeypatch, capsys):
    host = Host(tmp_path, monkeypatch)
    backup = BackupHost(tmp_path, monkeypatch)
    monkeypatch.setattr(p, "config", lambda path: host.settings)
    host.main = RELEASES[0]
    p.prepare(host.settings, RELEASES[0])
    backup.hold(until=20 * 60)
    p.main(["apply", "--sha", RELEASES[0]])
    printed, error = capsys.readouterr()
    result = json.loads(printed)
    assert result["status"] == "applied" and 20 * 60 <= result["backup_wait_seconds"] < 20 * 60 + 15
    assert p.read(host.state / RELEASES[0] / "result.json")["backup_wait_seconds"] == result["backup_wait_seconds"]
    assert error.count("backup check: waiting") == 1 and error.count("backup check: idle after") == 1
    assert not backup.lock_is_held()  # released when the command ends


# spec: ANYM-744
def test_standalone_apply_holds_the_lock_while_it_waits_for_calls(tmp_path, monkeypatch):
    host = Host(tmp_path, monkeypatch)
    backup = BackupHost(tmp_path, monkeypatch)
    monkeypatch.setattr(p, "config", lambda path: host.settings)
    host.main = RELEASES[0]
    p.prepare(host.settings, RELEASES[0])
    held = []
    monkeypatch.setattr(p, "wait_for_calls", lambda settings, deadline: held.append(backup.lock_is_held()))
    p.main(["apply", "--sha", RELEASES[0]])
    assert held and all(held)


# spec: ANYM-744
def test_standalone_apply_past_the_bound_exits_75_and_writes_nothing(tmp_path, monkeypatch, capsys):
    host = Host(tmp_path, monkeypatch)
    backup = BackupHost(tmp_path, monkeypatch)
    host.settings["backup"] = {"wait_seconds": 90}
    monkeypatch.setattr(p, "config", lambda path: host.settings)
    host.main = RELEASES[0]
    p.prepare(host.settings, RELEASES[0])
    host.calls.clear()
    backup.hold()
    with pytest.raises(SystemExit) as stopped:
        p.main(["apply", "--sha", RELEASES[0]])
    printed = capsys.readouterr().out
    assert stopped.value.code == 75 and printed.count("\n") == 1
    assert json.loads(printed) == {"status": "busy", "code": "backup_running", "unit": str(backup.lock),
                                   "waited_seconds": 90, "since": None}
    assert not (host.state / RELEASES[0] / "attempted.json").exists() and host.mutations() == []
    assert not p.PAUSE.exists() and backup.clock.now == 90


# spec: ANYM-744
def test_the_inner_admission_of_apply_never_waits_for_a_backup(tmp_path, monkeypatch):
    # deploy and the command line hold the lock before they call apply; the inner function neither takes nor waits.
    host = Host(tmp_path, monkeypatch)
    backup = BackupHost(tmp_path, monkeypatch)
    host.main = RELEASES[0]
    p.prepare(host.settings, RELEASES[0])
    backup.hold()
    monkeypatch.setattr(p, "wait_backup_idle", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("apply waited")))
    p.apply(host.settings, RELEASES[0])
    assert backup.clock.sleeps == [] and backup.probes == []


# spec: ANYM-744
def test_a_manual_deploy_waits_for_a_running_backup_then_deploys_and_the_wait_is_recorded(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    backup = BackupHost(tmp_path, monkeypatch)
    backup.hold(until=20 * 60)
    outcome = host.deploy()
    assert outcome["status"] == "deployed" and host.steps and 20 * 60 <= backup.clock.now < 20 * 60 + 15
    assert not backup.lock_is_held()


# spec: ANYM-744
def test_a_manual_deploy_with_no_backup_running_waits_zero_seconds(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    backup = BackupHost(tmp_path, monkeypatch)
    assert host.deploy()["status"] == "deployed" and backup.clock.sleeps == [] and backup.clock.now == 0


# spec: ANYM-744
def test_a_wrapper_held_auto_deploy_waits_zero_seconds_and_reads_no_unit(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    backup = BackupHost(tmp_path, monkeypatch)
    backup.hold()  # the unit's `flock --nonblock` wrapper holds it
    assert host.deploy(auto=True)["status"] == "deployed" and backup.clock.now == 0 and backup.probes == []


# spec: ANYM-744
@pytest.mark.parametrize("inner", ["apply", "promote"])
@pytest.mark.parametrize("auto", [False, True])
def test_deploys_real_inner_apply_and_promote_run_under_the_held_lock_and_never_wait(tmp_path, monkeypatch, auto, inner):
    # The real apply (or promote) runs, with its first remote gate replaced by a recorder that stops the deploy there:
    # whatever admission code the inner function has before that gate ran under deploy's lock, without waiting.
    from scripts import seamless_release as s
    real_apply, real_promote = p.apply, s.promote
    host = Deployer(tmp_path, monkeypatch)
    backup = BackupHost(tmp_path, monkeypatch)
    if auto:
        backup.hold()  # the unit's wrapper
    if inner == "apply":
        monkeypatch.setattr(p, "apply", real_apply)
    else:
        monkeypatch.setattr(s, "promote", real_promote)
        monkeypatch.setattr(s, "validate_settings", lambda value: value)
    waits, gates, real_wait = [], [], p.wait_backup_idle
    monkeypatch.setattr(p, "wait_backup_idle", lambda *args, **kwargs: waits.append(args) or real_wait(*args, **kwargs))

    def gate(sha):
        gates.append(backup.lock_is_held())
        raise p.Refused("stopped at the first remote gate")

    monkeypatch.setattr(p, "promotable", gate)
    outcome = host.deploy(auto=auto)
    assert outcome["status"] == "halted" and "stopped at the first remote gate" in outcome["reason"]
    assert gates == [True] and len(waits) == (0 if auto else 1)
    assert ("its HTTP release stopped" in outcome["reason"]) == (inner == "promote")
    assert backup.clock.sleeps == [] and backup.clock.now == 0


# spec: ANYM-744
@pytest.mark.parametrize("auto", [False, True])
def test_deploy_holds_the_lock_inside_apply_and_promote_and_waits_at_most_once(tmp_path, monkeypatch, auto):
    host = Deployer(tmp_path, monkeypatch)
    backup = BackupHost(tmp_path, monkeypatch)
    waits, held, real_wait = [], [], p.wait_backup_idle
    if auto:
        backup.hold()  # the unit's wrapper
    monkeypatch.setattr(p, "wait_backup_idle", lambda *args, **kwargs: waits.append(args) or real_wait(*args, **kwargs))
    for module, name in ((p, "apply"), (host.s, "promote")):
        inner = getattr(module, name)
        monkeypatch.setattr(module, name, lambda *args, _inner=inner, **kwargs:
                            (held.append(backup.lock_is_held()), _inner(*args, **kwargs))[1])
    assert host.deploy(auto=auto)["status"] == "deployed"
    assert held == [True, True] and len(waits) == (0 if auto else 1)


# spec: ANYM-744
def test_a_dry_run_deploy_never_waits_and_never_takes_the_lock(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    backup = BackupHost(tmp_path, monkeypatch)
    backup.hold()
    outcome = host.deploy(dry_run=True)
    assert outcome["status"] != "busy" and backup.clock.now == 0 and backup.probes == [] and host.steps == []


# spec: ANYM-744
def test_a_manual_deploy_past_the_bound_returns_busy_with_the_code_and_changes_nothing(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    host.settings["backup"] = {"wait_seconds": 45}
    backup = BackupHost(tmp_path, monkeypatch)
    backup.hold()
    outcome = host.deploy()
    assert outcome == {"status": "busy", "code": "backup_running", "unit": str(backup.lock), "waited_seconds": 45,
                       "since": None, "reason": f"backup_running: a backup holds {backup.lock}"}
    assert host.steps == [] and host.git == [] and host.requests == [] and host.paused() is None


# spec: ANYM-744
def test_the_command_line_exits_75_with_one_json_line_for_a_deploy_that_met_a_backup(monkeypatch, capsys):
    busy = {"status": "busy", "code": "backup_running", "unit": "personal-os-backup.service", "waited_seconds": 2700,
            "since": "Wed 2026-11-04 18:00:01 UTC", "reason": "backup_running: personal-os-backup.service is active"}
    monkeypatch.setattr(p, "execute_action", lambda args: busy)
    with pytest.raises(SystemExit) as stopped:
        p.main(["deploy"])
    printed = capsys.readouterr().out
    assert stopped.value.code == 75 and printed.count("\n") == 1 and json.loads(printed) == busy
    # Any other busy answer (the deploy lock) keeps its old exit status.
    monkeypatch.setattr(p, "execute_action", lambda args: {"status": "busy", "reason": "another production operation"})
    p.main(["deploy"])


# spec: ANYM-744
def test_production_md_names_the_wait_the_exit_code_and_the_windows_that_stay():
    docs = " ".join((ROOT / "docs/operations/production.md").read_text().split())  # line breaks are layout
    assert "backup.wait_seconds" in docs and "backup_running" in docs and "exit 75" in docs
    # Owner 11-04 (#617): no clock window; the wait still does not protect a scheduled start.
    assert "A release has no clock window" in docs and "the wait does not protect a scheduled start." in docs
    # The two real retry behaviours replace the blanket "systemd retries it".
    assert "at most three starts in 8 hours" in docs and "5400 s" in docs and "systemd retries it;" not in docs


def held_backup_lock(tmp_path, monkeypatch):
    """The backup runner's lock, held by a synthetic backup run (another open file description)."""
    path = tmp_path / "backups" / ".runner.lock"
    path.parent.mkdir(mode=0o700)
    monkeypatch.setenv("DEEDKIT_BACKUP_LOCK", str(path))
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return path, fd


# spec: ANYM-608
def test_a_manual_deploy_returns_busy_before_any_change_while_a_backup_holds_its_lock(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    host.settings["backup"] = {"wait_seconds": 0}  # ANYM-744: the default waits; 0 is the old single check
    path, fd = held_backup_lock(tmp_path, monkeypatch)
    BackupHost(tmp_path, monkeypatch)  # answers the unit reads: every unit idle
    try:
        outcome = host.deploy()
    finally:
        os.close(fd)
    assert outcome["status"] == "busy" and outcome["reason"].startswith("backup_running") and str(path) in outcome["reason"]
    assert host.steps == [] and host.git == [] and host.requests == [] and host.paused() is None
    assert not (host.state / "deploy.json").exists()
    # Once the backup ends, the same deploy runs and releases the lock when it ends.
    assert host.deploy()["status"] == "deployed"
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)


# spec: ANYM-608
def test_an_auto_deploy_under_the_units_held_wrapper_lock_is_not_blocked_by_it(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    _, fd = held_backup_lock(tmp_path, monkeypatch)  # the unit's `flock --nonblock` wrapper holds it
    try:
        outcome = host.deploy(auto=True)
    finally:
        os.close(fd)
    assert outcome["status"] == "deployed" and host.steps


# spec: ANYM-608
def test_a_host_without_the_backup_directory_deploys_without_the_backup_lock(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)  # no runner installed, no DEEDKIT_BACKUP_LOCK, no lock directory
    assert host.deploy()["status"] == "deployed" and not p.BACKUP_LOCK.parent.exists()


# spec: ANYM-608
@pytest.mark.parametrize("cause", ["override", "runner"])
def test_a_missing_lock_directory_never_skips_the_lock_when_named_or_when_a_runner_is_installed(tmp_path, monkeypatch,
                                                                                                cause):
    host = Deployer(tmp_path, monkeypatch)
    if cause == "override":
        monkeypatch.setenv("DEEDKIT_BACKUP_LOCK", str(tmp_path / "mistyped" / ".runner.lock"))
    else:
        monkeypatch.setattr(p, "BACKUP_LOCK", tmp_path / "no-backups" / ".runner.lock")
        p.BACKUP_RUNNER.parent.mkdir(parents=True)
        p.BACKUP_RUNNER.write_bytes(b"installed runner\n")
    outcome = host.deploy()
    assert outcome["status"] == "halted" and "cannot be opened" in outcome["reason"]
    assert host.steps == [] and host.git == []


# spec: ANYM-784
@pytest.mark.parametrize("auto", [False, True])
def test_a_deploy_returns_busy_while_a_calendar_rotation_is_unsettled(tmp_path, monkeypatch, auto):
    host = Deployer(tmp_path, monkeypatch)
    marker = tmp_path / "rotation.unsettled"
    monkeypatch.setattr(p, "ROTATION_MARKER", marker)
    marker.write_text('{"phase":"altered"}\n')  # its process may be long gone: the file is the admission
    outcome = host.deploy(auto=auto)
    assert outcome["status"] == "busy" and outcome["code"] == "rotation_running" and str(marker) in outcome["reason"]
    assert host.steps == [] and host.git == [] and host.paused() is None
    marker.unlink()
    assert host.deploy(auto=auto)["status"] == "deployed"


# spec: ANYM-784
def test_a_rotation_keeps_a_dry_run_from_planning_a_release_and_exits_without_a_pause(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    marker = tmp_path / "rotation.unsettled"
    monkeypatch.setattr(p, "ROTATION_MARKER", marker)
    marker.write_text('{"phase":"verifying"}\n')
    assert host.deploy(dry_run=True)["code"] == "rotation_running" and host.paused() is None


# spec: ANYM-608
@pytest.mark.parametrize("auto", [False, True])
def test_a_deploy_returns_busy_while_a_calendar_backup_worker_is_not_verified_stopped(tmp_path, monkeypatch, auto):
    host = Deployer(tmp_path, monkeypatch)
    host.settings["backup"] = {"wait_seconds": 0}  # ANYM-744: the default waits; 0 is the old single check
    lock = tmp_path / "backups" / ".runner.lock"
    lock.parent.mkdir(mode=0o700)
    monkeypatch.setenv("DEEDKIT_BACKUP_LOCK", str(lock))
    BackupHost(tmp_path, monkeypatch)  # answers the unit reads: every unit idle
    marker = tmp_path / "backups" / ".runner.lock.unsettled"
    marker.write_text('{"profile":"calendar"}\n')  # a calendar run failed its recovery and released the locks
    outcome = host.deploy(auto=auto)
    assert outcome["status"] == "busy" and outcome["reason"].startswith("backup_running") and str(marker) in outcome["reason"]
    assert host.steps == [] and host.git == [] and host.paused() is None
    marker.unlink()
    assert host.deploy(auto=auto)["status"] == "deployed"


# spec: ANYM-215
def test_reused_native_parent_placeholder_still_requires_successful_native_producer():
    original, _, _ = queue_github(push_native='skipped', queue_jobs=QUEUE_JOBS_WITH_NATIVE)
    def parent(get):
        def request(path):
            result = get(path)
            if 'runs/40/attempts/1/jobs?' in path:
                result = copy.deepcopy(result)
                for job in result['jobs']:
                    if job['name'] == p.NATIVE_JOB:
                        job['name'] = 'native'
            return result
        return request
    assert p.verify(SHA, parent(original))['verify_run_id'] == 30
    jobs = [job for job in QUEUE_JOBS_WITH_NATIVE if job['name'] != p.NATIVE_JOB]
    missing, _, _ = queue_github(push_native='skipped', queue_jobs=jobs)
    with pytest.raises(p.Refused):
        p.verify(SHA, parent(missing))
    plain, _, _ = queue_github(push_verify='success', push_native='skipped', queue_jobs=QUEUE_JOBS_WITH_NATIVE)
    with pytest.raises(p.Refused):
        p.verify(SHA, parent(plain))
