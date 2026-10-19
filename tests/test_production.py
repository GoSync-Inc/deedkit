"""Promotion guards use synthetic GitHub/Compose responses; no live runtime access."""
import contextlib
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from types import SimpleNamespace
import urllib.error

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
    """No test reads this machine's production state, /etc/deedkit or containerd directory."""
    for name in ("HOST_STATE", "CONFIG_DIRECTORY", "CONTAINERD_ROOT"):
        monkeypatch.setattr(p, name, tmp_path / "absent" / name.lower())


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
    assert gate["needs"] == ["reuse", "pr_reuse", "checks", "native", "images"] and gate["if"] == "${{ always() }}"
    assert workflow["jobs"]["native"]["uses"] == "./.github/workflows/whatsmeow.yml"
    script = gate["steps"][0]["run"]
    for checks, native, images in [("success", "success", "success"), ("failure", "success", "success"),
                                  ("success", "skipped", "success"), ("cancelled", "success", "success"),
                                  ("success", "success", "failure")]:
        result = subprocess.run(["bash", "-e", "-c", script], env={"CHECKS_RESULT": checks, "NATIVE_RESULT": native, "IMAGES_RESULT": images}, capture_output=True)
        assert (result.returncode == 0) == (checks == native == images == "success")


def test_ci_gate_refuses_a_manual_baseline_of_another_revision():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    step = workflow["jobs"]["gate"]["steps"][0]
    assert step["env"]["SOURCE_REF"] == "${{ inputs.source_ref || '' }}"
    assert step["env"]["EXPECTED_SHA"] == "${{ github.sha }}"
    green = {"CHECKS_RESULT": "success", "NATIVE_RESULT": "success", "IMAGES_RESULT": "success",
             "EXPECTED_SHA": SHA}
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


def queue_github(*, push_verify="skipped", queue=None, queue_jobs=None, extra_queue_runs=(), pages=None, changes=None):
    """A main push run that skipped checks / verify plus the merge queue run of the same commit."""
    push = {"id": 40, "run_attempt": 1, "head_sha": SHA, "head_branch": "main", "event": "push", "status": "completed",
            "conclusion": "success", "head_repository": {"full_name": p.REPOSITORY}, "html_url": "https://example.invalid/run/40"}
    group = {"id": 30, "run_attempt": 2, "head_sha": SHA, "head_branch": "gh-readonly-queue/main/pr-86-" + "c" * 40,
             "event": "merge_group", "status": "completed", "conclusion": "success",
             "head_repository": {"full_name": p.REPOSITORY}, "html_url": "https://example.invalid/run/30", **(queue or {})}
    push_jobs = [{"name": name, "status": "completed",
                  "conclusion": push_verify if name == "checks / verify" else "success"} for name in p.REQUIRED_JOBS]
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
    assert p.PUBLISHED == publish_images.TARGETS and set(p.BUILDS) - set(p.PUBLISHED) == {"http-router"}
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


def rolled_back_refusal(host, match):
    host.calls.clear()
    with pytest.raises(p.Refused, match=match):
        p.rollback(host.settings, RELEASES[1])
    assert host.mutations() == [] and not (host.state / RELEASES[1] / "rollback.json").exists()


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
    p.write(state / "current.json", p.read(state / sha / "result.json"))
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
    p.write(state / sha / "prepared.json", {"sha": sha, "images": {"runtime": image}})
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
        self.blobs, self.git_failures = {}, {}
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
        self.retained = []
        def retain(settings, *, held=()):
            self.retained.append(held)
            return {"removed": [], "failed": [], "kept": 2}
        monkeypatch.setattr(p, "retain_images", retain)

    def current(self, sha):
        p.write(self.state / "current.json", {"sha": sha, "status": "applied", "ci": {"url": "https://example.invalid"},
                                              "rollback": {"version": 1, "projects": [
                                                  {"name": "synthetic", "after_digest": p.digest(self.document)}]}})
        (self.state / sha).mkdir(exist_ok=True)
        p.write(self.state / sha / "prepared.json", {"sha": sha, "config_digest": p.digest(self.settings)})

    def step(self, http, name):
        label = ("seamless " if http else "") + name
        def call(settings, sha):
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
            return "\0".join(path for sha in commits for path in self.changes.get(sha, ())) + "\0"
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


def test_deploy_waits_for_the_api_quota_and_for_the_interval_between_automatic_deploys(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    # No API quota is read or waited for (ANYM-406): the harness fails any GitHub API request.
    assert not hasattr(p, "quota") and host.deploy()["status"] == "deployed"
    host.green.add("3" * 40)
    host.history.append("3" * 40)
    host.steps.clear()
    assert host.deploy(auto=True)["status"] == "waiting" and host.steps == []
    assert host.deploy()["sha"] == "3" * 40  # a manual run is not held back by the interval


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
def test_the_autodeploy_unit_runs_deploy_auto_and_never_starts_during_or_just_before_the_backup(tmp_path):
    lines = autodeploy_unit()
    timer = (ROOT / "deploy/deedkit-autodeploy.timer").read_text()
    assert "/usr/local/lib/deedkit/production.py deploy --auto " in lines["ExecStart"]
    assert "personal-os-backup.service" in lines["After"] and "OnUnitInactiveSec=5min" in timer

    def skipped(*arguments, **failures):
        return admission_skips(tmp_path / "bin", *arguments, **failures)

    assert not skipped("inactive", "12") and not skipped("failed", "19") and not skipped("inactive", "00")
    assert skipped("activating", "12") and skipped("active", "03") and skipped("deactivating", "12")
    assert skipped("inactive", "16") and skipped("inactive", "17") and skipped("inactive", "18")
    # A failed or unexpected read never admits a deploy.
    assert skipped("inactive", "12", systemctl_fails=True) and skipped("inactive", "12", date_fails=True)
    assert skipped("", "12") and skipped("inactive", "garbage") and skipped("inactive", "24")


# spec: ANYM-407
def test_a_deploy_admitted_before_the_backup_window_ends_before_the_daily_backup_starts(tmp_path):
    # personal-os-backup.timer starts the backup at 18:00 UTC; a deploy holding the runner lock then would fail
    # the backup and its retries (StartLimitBurst=3 in 8 hours, 30 minutes apart) until the backup is lost for the day.
    lines = autodeploy_unit()
    seconds = {"h": 3600, "min": 60, "s": 1}

    def duration(value):
        number, unit = re.fullmatch(r"(\d+)(h|min|s)", value).groups()
        return int(number) * seconds[unit]

    admitted = [hour for hour in range(18) if not admission_skips(tmp_path / "bin", "inactive", f"{hour:02d}")]
    latest_start = (admitted[-1] + 1) * 3600 - 1  # the last second of the last admitted hour before 18:00
    # The single ExecStart follows the hour read at once and gets TimeoutStartSec; then systemd stops it within TimeoutStopSec.
    latest_end = latest_start + duration(lines["TimeoutStartSec"]) + duration(lines["TimeoutStopSec"])
    assert latest_end < 18 * 3600 - 30 * 60  # half an hour before the backup, whatever the run's length


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


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the permission bits that stand in for a read-only mount")
@pytest.mark.parametrize("sidecars", [False, True], ids=["wal-without-sidecars", "wal-with-sidecars"])
# spec: ANYM-442
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
    Each project runs its own container; `docker exec` answers the calls probe from `answers` in turn (the last one
    repeats). Time is a fake clock."""

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

    def voice_container(self, identity, image):
        return {"Id": identity, "Image": image, "State": {"Running": True},
                "Config": {"Env": ["VOICE_DATA_DIR=/data", "SYNTHETIC=literal"], "User": "10001:10001"},
                "Mounts": [{"Type": "bind", "Source": str(self.root / "voice-data"), "Destination": "/data"}]}

    def promotable(self, sha):
        self.events.append("promotable")
        return {"sha": sha, "run_id": 12}

    def answer(self):
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer if isinstance(answer, str) else json.dumps({"calls": [list(call) for call in answer]})

    def run(self, argv, **kwargs):
        label = next((item.rpartition("=")[2] for item in argv if item.startswith("label=com.docker.compose.project=")),
                     None)
        if argv[:2] == ["docker", "run"]:
            assert p.CALLS_PROBE in argv and 5 <= kwargs.get("timeout") <= p.CALL_PROBE
            attempted = sorted(path.parent.name for path in self.state.glob("*/*attempted.json"))
            self.events.append(("probe", self.inspected, attempted))
            return self.answer()
        if argv[:2] == ["docker", "compose"]:
            self.calls.append(argv)
            name = argv[argv.index("--project-name") + 1]
            container = self.containers[name]
            if "config" in argv:
                return json.dumps(json.loads(Path(argv[argv.index("-f") + 1]).read_text())).replace("$$", "$")
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
    assert host.events[:6] == [("probe", "voice0", []), ("probe", "voice0", []), ("probe", "voice0", []),
                               "promotable", ("probe", "voice0", []), ("stop", "synthetic")]
    assert host.clock.sleeps == [p.CALL_POLL, p.CALL_POLL]
    # Before the second project's stop it checks again, with the marker written.
    assert host.events[6:] == [("probe", "voice0", [RELEASES[0]]), ("stop", "personal-os-voice")]
    assert f"calls check: waiting for a call to end: {CALL} (active)" in capsys.readouterr().err


# spec: ANYM-442
def test_a_call_starting_during_the_last_ci_check_is_waited_for_before_anything_is_written(tmp_path, monkeypatch):
    host = CallHost(tmp_path, monkeypatch, answers=((), [(CALL, "ringing")], ()))
    host.prepared(RELEASES[0])
    p.apply(host.settings, RELEASES[0])
    assert host.events[:5] == [("probe", "voice0", []), "promotable", ("probe", "voice0", []),
                               ("probe", "voice0", []), ("stop", "synthetic")]


# spec: ANYM-442
def test_a_call_that_outlasts_the_bound_refuses_apply_before_any_change(tmp_path, monkeypatch, capsys):
    host = CallHost(tmp_path, monkeypatch, answers=([(CALL, "waiting_for_browser")],))
    host.prepared(RELEASES[0])
    host.calls.clear()
    with pytest.raises(p.CallActive, match=re.escape(f"a call is active: {CALL} (waiting_for_browser)")):
        p.apply(host.settings, RELEASES[0])
    # It waited out the bound, never longer, then stopped nothing and wrote nothing in the release.
    assert host.clock.now == p.CALL_WAIT and "promotable" not in host.events
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
    assert host.events == [("probe", "voice0", []), ("probe", "voice0", []), ("stop", "synthetic"),
                           ("probe", "voice1", [RELEASES[1]]), ("stop", "personal-os-voice")]


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
# spec: ANYM-442
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
        return json.dumps({"calls": [[CALL if "type=bind" in " ".join(argv) else OTHER_CALL, "active"]]})
    monkeypatch.setattr(p, "run", run)
    settings = {"projects": [{"name": "personal-os-probe", "services": {"telegram": "runtime"}},
                             {"name": "personal-os-voice", "services": {"voice": "calls"}},
                             {"name": "second", "services": {"calls": "calls", "api": "runtime"}},
                             {"name": "stopped", "services": {"voice": "calls"}}]}
    monkeypatch.setattr(p, "time", Clock())
    assert p.active_calls(settings, p.CALL_WAIT) == [f"{CALL} (active)", f"{OTHER_CALL} (active)"]
    assert [argv[:2] for argv, _ in commands] == [["docker", "ps"], ["docker", "inspect"], ["docker", "run"]] * 2 + [
        ["docker", "ps"]]
    assert all(kwargs == {"timeout": p.CALL_PROBE} for _, kwargs in commands)
    probe = commands[2][0]
    image = probe.index(IMAGE)
    assert probe[image + 1:] == ["-I", "-c", p.CALLS_PROBE, str(p.CALL_READ), str(p.CALL_LIFETIME), *p.CALL_STATUSES]
    options = probe[2:image]
    for flag in (["--rm"], ["--pull", "never"], ["--network", "none"], ["--read-only"], ["--cap-drop", "ALL"],
                 ["--memory", "64m"], ["--pids-limit", "16"], ["--user", "10001:10001"],
                 ["--mount", "type=bind,source=/opt/personal-os-voice/data,target=/data,readonly"],
                 ["--env", "VOICE_DATA_DIR=/data"], ["--entrypoint", "python"]):
        assert any(options[i:i + len(flag)] == flag for i in range(len(options))), flag
    # Nothing of the calls container's environment reaches the probe.
    assert not any("SECRET" in item for item in probe)
    assert "type=volume,source=second_calls,target=/data,readonly" in commands[5][0]
    # A configuration without a calls target checks nothing.
    commands.clear()
    assert p.active_calls({"projects": settings["projects"][:1]}, p.CALL_WAIT) == [] and commands == []


# spec: ANYM-442
def test_a_probe_the_client_gave_up_on_is_killed_by_name(monkeypatch):
    commands = []
    def run(argv, **kwargs):
        commands.append(argv)
        if argv[:2] == ["docker", "ps"]:
            return "c1\n"
        if argv[:2] == ["docker", "inspect"]:
            return json.dumps([{"Image": IMAGE, "Config": {}, "Mounts": [
                {"Type": "bind", "Source": "/srv/calls", "Destination": "/data"}]}])
        if argv[:2] == ["docker", "run"]:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return ""
    monkeypatch.setattr(p, "run", run)
    monkeypatch.setattr(p, "time", Clock())
    with pytest.raises(p.CallsUnknown, match="probe_timeout"):
        p.active_calls({"projects": [{"name": "personal-os-voice", "services": {"voice": "calls"}}]}, p.CALL_WAIT)
    name = commands[2][commands[2].index("--name") + 1]
    assert re.fullmatch(r"deedkit-calls-probe-[0-9a-f]{12}", name) and commands[3] == ["docker", "kill", name]


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


@pytest.mark.parametrize("refusal", [p.CallActive(f"a call is active: {CALL} (active)"),
                                     p.CallsUnknown("whether a call is active could not be read: busy")],
                         ids=["call-active", "calls-unknown"])
# spec: ANYM-442
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
