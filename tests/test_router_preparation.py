"""Router enrollment builds verified fetched source, never a dirty checkout."""
import json
from pathlib import Path
import stat
import subprocess

import pytest

from scripts import production as p

SHA = "a" * 40
IMAGE = "sha256:" + "b" * 64


def fixture(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    settings = {"state_directory": str(state)}
    calls = []
    def command(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[:2] == ["docker", "info"]:
            return str(tmp_path)
        if "FETCH_HEAD" in argv:
            return SHA
        return ""
    monkeypatch.setattr(p, "run", command)
    monkeypatch.setattr(p, "check_space", lambda path: None)
    monkeypatch.setattr(p, "promotable", lambda sha: {"sha": sha, "run_id": 123})
    monkeypatch.setattr(p, "build_image", lambda source, sha, target: IMAGE)
    return settings, calls, state


def test_router_ci_failure_does_not_fetch_build_or_write_receipt(tmp_path, monkeypatch):
    settings, calls, state = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(p, "promotable", lambda sha: (_ for _ in ()).throw(p.Refused("CI failed")))
    with pytest.raises(p.Refused, match="CI failed"):
        p.prepare_router(settings, SHA)
    assert calls == [] and list(state.iterdir()) == []


def test_router_main_movement_prevents_build_and_enrollment_receipt(tmp_path, monkeypatch):
    settings, calls, state = fixture(tmp_path, monkeypatch)
    command = p.run
    monkeypatch.setattr(p, "run", lambda argv, **kw: "c" * 40 if "FETCH_HEAD" in argv else command(argv, **kw))
    monkeypatch.setattr(p, "build_image", lambda *args: pytest.fail("main moved; must not build"))
    with pytest.raises(p.Refused, match="not the verified candidate commit"):
        p.prepare_router(settings, SHA)
    assert not (state / ("router-" + SHA) / "router-build.json").exists()


def test_router_final_ci_failure_never_creates_authorizing_receipt(tmp_path, monkeypatch):
    settings, calls, state = fixture(tmp_path, monkeypatch)
    attempts = []
    def verify(sha):
        attempts.append(sha)
        if len(attempts) == 2:
            raise p.Refused("new CI failed")
        return {"sha": sha}
    monkeypatch.setattr(p, "promotable", verify)
    with pytest.raises(p.Refused, match="new CI failed"):
        p.prepare_router(settings, SHA)
    assert attempts == [SHA, SHA]
    assert not (state / ("router-" + SHA) / "router-build.json").exists()
    with pytest.raises(p.Refused, match="already exists"):
        p.prepare_router(settings, SHA)


@pytest.mark.parametrize("sha", ["main", "a" * 12, "A" * 40, "../../outside"])
def test_router_ambiguous_source_cannot_create_state(tmp_path, monkeypatch, sha):
    settings, calls, state = fixture(tmp_path, monkeypatch)
    with pytest.raises(p.Refused):
        p.prepare_router(settings, sha)
    assert not list(state.iterdir())


def test_router_unidentified_image_cannot_authorize_bootstrap(tmp_path, monkeypatch):
    settings, calls, state = fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(p, "build_image", lambda *args: "router:latest")
    with pytest.raises(p.Refused, match="immutable"):
        p.prepare_router(settings, SHA)
    assert not (state / ("router-" + SHA) / "router-build.json").exists()


def test_router_build_fetches_committed_main_instead_of_dirty_checkout(tmp_path, monkeypatch):
    origin = tmp_path / "dirty-checkout"
    origin.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", "-C", str(origin), *args], text=True).strip()
    git("init", "--quiet", "--initial-branch=main")
    (origin / "source.txt").write_text("committed synthetic source\n")
    git("add", "source.txt")
    git("-c", "user.name=Synthetic", "-c", "user.email=fixture@example.invalid", "commit", "--quiet", "-m", "fixture")
    sha = git("rev-parse", "HEAD")
    (origin / "source.txt").write_text("unverified dirty source\n")
    (origin / "untracked.txt").write_text("must not enter image\n")
    settings, calls, state = fixture(tmp_path, monkeypatch)
    monkeypatch.chdir(origin)
    def command(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[:2] == ["docker", "info"]:
            return str(tmp_path)
        if "remote" in argv and "add" in argv:
            argv = [*argv[:-1], str(origin)]
        return subprocess.check_output(argv, text=True, umask=kwargs.get("umask", -1)).strip()
    monkeypatch.setattr(p, "run", command)
    built = []
    def build(source, revision, target):
        assert source == state / ("router-" + sha) / "source"
        assert source != origin and revision == sha and target == "http-router"
        assert (source / "source.txt").read_text() == "committed synthetic source\n"
        assert not (source / "untracked.txt").exists()
        built.append(source)
        return IMAGE
    monkeypatch.setattr(p, "build_image", build)
    receipt = p.prepare_router(settings, sha)
    assert len(built) == 1
    assert receipt == {"version": 1, "target": "http-router", "sha": sha, "image": IMAGE,
                       "ci": {"sha": sha, "run_id": 123}}
    path = state / ("router-" + sha) / "router-build.json"
    assert json.loads(path.read_text()) == receipt
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert all("compose" not in argv and "stop" not in argv for argv, _ in calls)
    assert any("checkout" in argv and kw.get("umask") == 0o022 for argv, kw in calls)
