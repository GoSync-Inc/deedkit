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
from types import SimpleNamespace
import urllib.error

import pytest
import yaml

from scripts import production as p

SHA = "a" * 40
IMAGE = "sha256:" + "b" * 64
OLD_IMAGE = "sha256:" + "d" * 64
ROOT = Path(__file__).resolve().parents[1]


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
    monkeypatch.setattr(p, "verify", lambda sha: {"sha": sha, "run_id": 12})
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
    monkeypatch.setattr(p, "verify", gate)
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
        monkeypatch.setattr(p, "verify", lambda sha: (_ for _ in ()).throw(p.Refused("red CI")))
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
        monkeypatch.setattr(p, "verify", lambda sha: {"sha": sha, "run_id": 12})
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
    return {"version": 1, "state_directory": str(state), "projects": []}, calls


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
    assert kept[image_id("4")] == ["rollback target of the current release", f"one of the two newest other releases ({sha[3]})"]
    assert kept[image_id("3")] == [f"one of the two newest other releases ({sha[2]})"]
    assert kept[image_id("5")] == [f"release {sha[4]} without applied_at, order unknown"]
    assert kept[image_id("7")] == [f"prepared release {sha[6]} is not applied"]
    assert kept[image_id("1")] == ["used by container stopped0"]
    # Only the eight CI-published targets' production tags are reported; the second release is the one candidate.
    assert report["candidates"] == [{"image": image_id("2"), "reason": "no current, rollback, recent, prepared or container use",
                                     "tags": [f"deedkit-production-runtime:{sha[1]}", "deedkit-production-runtime:extra"]}]
    assert set(kept) | {image_id("2")} == {image_id(str(n)) for n in range(1, 8)}
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
    assert set(kept) == {host.image(sha) for sha in RELEASES}


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
        return lambda path: answer if path.startswith("compare/main...") else get(path)
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
        assert [path.split("?")[0] + ("" if "?" not in path else "?" + re.search(r"event=\w+", path).group(0))
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


def test_the_quota_a_deploy_waits_for_covers_every_verification_of_plans_a_and_b():
    import ast
    import inspect
    import textwrap
    from scripts import seamless_release as s
    def verifications(function):
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
        return sum(isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) == "verify"
                   for node in ast.walk(tree))
    # seamless prepare runs production.py prepare first.
    plan_a = verifications(p.prepare) + verifications(p.apply)
    plan_b = verifications(p.prepare) + verifications(s.prepare) + verifications(s.promote)
    assert (plan_a, plan_b) == (4, 6)
    get, _, _ = queue_github()  # the costliest path: the push run reused the queue's verification
    for steps, count, listing in (("plan A and B", plan_a + plan_b, 1), ("plan B", plan_b, 0)):
        paths = []
        with p.verification_reuse():
            for _ in range(count):
                p.verify(SHA, lambda path: paths.append(path) or get(path))
        assert len(paths) + listing <= p.QUOTA[steps] - 4, (steps, len(paths))  # 4 spare for a second page


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
    """A synthetic host for `deploy`: plan A and HTTP state at BASE, a main history BASE..NEWEST, a CI listing,
    and the release steps recorded instead of run. No git, GitHub or Docker access."""

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
        self.runs = [{"id": 100 + index, "head_sha": sha, "head_branch": "main", "event": "push",
                      "status": "completed" if sha in green else "in_progress",
                      "conclusion": "success" if sha in green else None, "html_url": f"https://example.invalid/{index}"}
                     for index, sha in enumerate(self.history)]
        self.steps, self.requests, self.git, self.remaining = [], [], [], 60
        self.blobs = {}
        self.current(BASE)
        p.write(self.http_state / "http-current.json",
                {"sha": BASE, "status": "serving", "config_digest": p.digest(self.http)})
        monkeypatch.setattr(p, "PAUSE", tmp_path / "autodeploy.pause")
        monkeypatch.setattr(s, "GLOBAL_LOCK", tmp_path / "global")
        monkeypatch.setattr(p, "run", self.run)
        monkeypatch.setattr(p, "quota", lambda: (self.remaining, "12:00:00Z"))
        for module, name in ((p, "prepare"), (p, "apply"), (p, "rollback"), (s, "prepare"), (s, "promote"),
                             (s, "retire")):
            monkeypatch.setattr(module, name, self.step(module is s, name))

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

    def get(self, path):
        self.requests.append(path)
        assert path.startswith("actions/workflows/ci.yml/runs?") and "branch=main" in path and "event=push" in path
        return {"workflow_runs": self.runs[::-1]}

    def run(self, argv, **kwargs):
        if argv[:2] == ["docker", "compose"] and "config" in argv:
            return json.dumps(self.document)
        assert argv[0] == "git", argv
        self.git.append(argv)
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
        return p.deploy(self.settings, self.http, get=self.get, **options)

    def paused(self):
        return json.loads(p.PAUSE.read_text())["reason"] if p.PAUSE.exists() else None


def test_deploy_applies_the_newest_green_main_commit_with_plan_a_then_plan_b(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch, history=(NEWER, NEWEST, "3" * 40), green=(NEWER, NEWEST))
    outcome = host.deploy()
    # The newest commit's CI still runs: the newest green one is deployed, everything at one SHA.
    assert host.steps == [("prepare", NEWEST), ("apply", NEWEST), ("seamless prepare", NEWEST),
                          ("seamless promote", NEWEST)]
    assert outcome["status"] == "deployed" and outcome["sha"] == NEWEST and outcome["base"] == BASE
    assert outcome["run"] == "https://example.invalid/2" and outcome["http"] == "serving"
    assert p.read(host.state / "deploy.json") == outcome and host.paused() is None
    assert len(host.requests) == 1
    # The next run waits for the newest commit's CI, with one listing.
    host.requests.clear()
    assert host.deploy()["status"] == "waiting" and len(host.requests) == 1 and len(host.steps) == 4


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
    for error in (TimeoutError(), p.RateLimited("GitHub rate limit exhausted, resets at 12:00:00Z")):
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
    host.remaining = p.QUOTA["plan A and B"] - 1
    outcome = host.deploy()
    assert outcome["status"] == "waiting" and "until 12:00:00Z" in outcome["reason"] and host.requests == []
    host.remaining = 60
    assert host.deploy()["status"] == "deployed"
    host.runs.append({"id": 200, "head_sha": "3" * 40, "head_branch": "main", "status": "completed",
                      "conclusion": "success", "html_url": "https://example.invalid/3"})
    host.history.append("3" * 40)
    host.steps.clear()
    assert host.deploy(auto=True)["status"] == "waiting" and host.steps == []
    assert host.deploy()["sha"] == "3" * 40  # a manual run is not held back by the interval


def test_deploy_finishes_plan_b_when_plan_a_is_ahead_of_the_serving_http_release(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    host.current(NEWER)  # plan A applied NEWER; its HTTP release waited for the API quota
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


def test_a_dry_run_reports_the_plan_or_the_pause_reason_and_changes_nothing(tmp_path, monkeypatch):
    host = Deployer(tmp_path, monkeypatch)
    assert host.deploy(dry_run=True) == {"status": "plan", "sha": NEWEST, "base": BASE,
                                         "run": "https://example.invalid/2", "steps": ["plan A", "plan B"]}
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
    monkeypatch.setattr(p, "api", host.get)
    with pytest.raises(SystemExit) as stopped:
        p.main(arguments)
    printed = json.loads(capsys.readouterr().out)
    assert stopped.value.code == 1 and printed["status"] == "halted" and "scripts/production.py" in printed["reason"]
    p.main(arguments)  # paused now: one quiet line, exit 0
    assert json.loads(capsys.readouterr().out)["status"] == "paused"
