"""One release drill for one commit: version tag, artifacts, Tiden evidence and release record. Nothing is uploaded."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import tomllib
import zipfile

import yaml

from scripts.tiden_ci import MODULE_REPORTS, REQUIRED_EXECUTIONS
from tests.test_tiden_ci import (ATTEMPT_URL, RUN_URL, evidence_fragments, main_push_manifest, main_run, node_report, release_cli,
                                 run_release)
from tests.test_ci_reuse import go_stream

ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_GIT = {"GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "Synthetic", "GIT_AUTHOR_EMAIL": "synthetic@example.invalid",
                 "GIT_COMMITTER_NAME": "Synthetic", "GIT_COMMITTER_EMAIL": "synthetic@example.invalid"}
VERIFY_REPORTS = ("collector.xml", "npm.xml", "gateway.xml", "platform-contracts.xml", "calendar.xml", "calls.xml",
                  "calls-frontend.xml", "backup-restore.xml")
PASSING_CASE = '<testsuite><testcase name="x" file="tests/x.py"/></testsuite>'
FAILING_CASE = '<testsuite><testcase name="x" file="tests/x.py"><failure/></testcase></testsuite>'


def run(args, cwd, env=None, input=None, timeout=600):
    return subprocess.run(args, cwd=cwd, env=env, input=input, capture_output=True, timeout=timeout)


def git_output(repository, *args):
    env = {"PATH": os.environ["PATH"], "HOME": str(repository.parent), **SYNTHETIC_GIT}
    result = run(["git", "-C", str(repository), *args], repository, env)
    assert result.returncode == 0, result.stderr
    return result.stdout


def git(repository, *args):
    return git_output(repository, *args).decode().strip()


def archived_repository(tmp_path):
    """HEAD as `git archive` ships it, committed on main with origin/main and the release tag set."""
    head = git(ROOT, "rev-parse", "HEAD")
    version = tomllib.loads((ROOT / "services/messaging/pyproject.toml").read_text())["project"]["version"]
    archive = git_output(ROOT, "archive", "--format=tar", "HEAD")
    archived_id = run(["git", "get-tar-commit-id"], tmp_path, input=archive).stdout.decode().strip()
    repository = tmp_path / "drill"
    repository.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive)) as tree:
        tree.extractall(repository, filter="data")
    git(repository, "init", "--quiet", "--initial-branch=main")
    git(repository, "add", "--all")
    git(repository, "commit", "--quiet", "-m", f"Release drill of {head}")
    commit = git(repository, "rev-parse", "HEAD")
    git(repository, "update-ref", "refs/remotes/origin/main", commit)
    git(repository, "tag", f"v{version}", commit)
    return repository, version, head, archived_id, commit


def tag_guard(kind):
    steps = yaml.safe_load((ROOT / f".github/workflows/publish-{kind}.yml").read_text())["jobs"]["tag"]["steps"]
    guard = next(step for step in steps if step.get("name") == "Require the version tag")
    assert guard["env"] == {"VERSION": "${{ inputs.version }}", "REF": "${{ github.ref }}", "SHA": "${{ github.sha }}"}
    return guard["run"]


def guard_passes(script, repository, version, sha):
    env = {"PATH": os.environ["PATH"], "HOME": str(repository.parent), "GIT_CONFIG_NOSYSTEM": "1",
           "VERSION": version, "REF": f"refs/tags/v{version}", "SHA": sha}
    return run(["bash", "-e", "-c", script], repository, env, timeout=30).returncode == 0


def check_release(repository, version):
    return run([sys.executable, "scripts/check_release.py", version], repository)


def rewritten_wheel(wheel):
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("messaging_service/private.dsn", "synthetic")


def rewritten_npm_version(tarball):
    with tarfile.open(tarball) as source:
        members = [(member, source.extractfile(member)) for member in source.getmembers()]
        contents = [(member, handle.read() if handle else b"") for member, handle in members]
    with tarfile.open(tarball, "w:gz") as target:
        for member, data in contents:
            if member.name == "package/package.json":
                data = json.dumps({**json.loads(data), "version": "0.0.0"}).encode()
                member.size = len(data)
            target.addfile(member, io.BytesIO(data) if member.isfile() else None)


def evidence_for(tmp_path, name, commit, reports):
    """Synthetic fragments of every producer for `commit`, assembled and prepared by the repository's own tool."""
    # The drill prepares against the archived tree, so the Go streams name its real modules and tests.
    fragments = evidence_fragments(tmp_path / name, verify_files=VERIFY_REPORTS,
                                   go_streams={check: go_stream(check) for check in ("whatsmeow", "release_router")})
    for path in fragments.rglob("*"):
        if path.is_file():
            path.write_text(path.read_text().replace("a" * 40, commit))
    for module, required in REQUIRED_EXECUTIONS.items():  # each holds exactly its required executions
        (fragments / "ci-evidence-verify" / MODULE_REPORTS[module][0]).write_text(node_report(*sorted(required)))
    for fragment, report in reports.items():
        (fragments / "ci-evidence-verify" / fragment).write_text(report)
    producers = [f"{job}:success:{commit}" for job in ("verify", "load", "python-2", "python-3", "python-4", "python-5", "python-6", "go", "added")]
    env = {"PATH": os.environ["PATH"], "GITHUB_REPOSITORY": "example/repo", "GITHUB_RUN_ID": "1", "GITHUB_RUN_ATTEMPT": "2"}
    directory = tmp_path / f"{name}-evidence"
    tool = ["scripts/tiden_ci.py"]
    steps = [[*tool, "assemble", "--directory", str(directory), "--fragments", str(fragments), "--source-root", ".",
              *(a for p in producers for a in ("--producer", p))],
             [*tool, "prepare", "--directory", str(directory), "--source-root", "."]]
    for step in steps:
        done = run([sys.executable, *step], tmp_path / "drill", {**env, "GITHUB_EVENT_NAME": "push"})
        assert done.returncode == 0, done.stdout + done.stderr
    manifest = json.loads((directory / "manifest.json").read_text())
    verdict = run([sys.executable, "scripts/tiden_ci.py", "verdict", "--directory", str(directory)], tmp_path / "drill", env)
    return manifest, verdict


# spec: ANYM-418
def test_release_drill_one_version_and_one_commit_from_tag_to_tiden_release_record(tmp_path, monkeypatch):
    repository, version, head, archived_id, commit = archived_repository(tmp_path)
    # One commit: the archive was made from HEAD, and the throwaway commit, its tag and its tree record it.
    assert archived_id == head
    assert git(repository, "rev-parse", f"v{version}^{{commit}}") == commit
    assert head in git(repository, "log", "-1", "--format=%B")
    assert git(repository, "rev-parse", "HEAD^{tree}") == git(ROOT, "rev-parse", "HEAD^{tree}")

    # The tag guard of both publish workflows accepts the tag and refuses a tag moved after the dispatch.
    moved = tmp_path / "moved"
    shutil.copytree(repository, moved)
    git(moved, "commit", "--quiet", "--allow-empty", "-m", "moved")
    git(moved, "update-ref", "refs/remotes/origin/main", git(moved, "rev-parse", "HEAD"))
    git(moved, "tag", "--force", f"v{version}")
    for kind in ("pypi", "npm"):
        assert guard_passes(tag_guard(kind), repository, version, commit), kind
        assert not guard_passes(tag_guard(kind), moved, version, commit), kind

    # The stable build entrypoint, run in the drill repository, builds the wheel, the sdist and both npm tarballs.
    built = run([sys.executable, "scripts/build.py"], repository)
    assert built.returncode == 0, built.stderr
    dist = repository / "dist"
    assert sorted(path.name for path in dist.iterdir() if not path.name.startswith(".")) == sorted([
        f"anymsg-{version}-py3-none-any.whl", f"anymsg-{version}.tar.gz", f"anymsg-{version}.tgz", f"deedkit-{version}.tgz"])
    accepted = check_release(repository, version)
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert check_release(repository, "0.0.0").returncode != 0
    for name, damage in ((f"anymsg-{version}-py3-none-any.whl", rewritten_wheel), (f"anymsg-{version}.tgz", rewritten_npm_version)):
        original = (dist / name).read_bytes()
        damage(dist / name)
        assert check_release(repository, version).returncode != 0, name
        (dist / name).write_bytes(original)
    assert check_release(repository, version).returncode == 0

    # The wheel installs outside the repository and reports the tagged version.
    environment = tmp_path / "installed"
    uv = {**os.environ, "UV_NO_PROGRESS": "1"}
    for command in (["uv", "venv", str(environment), "--python", sys.executable],
                    ["uv", "pip", "install", "--python", str(environment / "bin/python"),
                     str(dist / f"anymsg-{version}-py3-none-any.whl")]):
        installed = run(command, tmp_path, uv)
        assert installed.returncode == 0, installed.stderr
    reported = run([str(environment / "bin/python"), "-m", "anymsg", "--version"], tmp_path)
    assert reported.stdout.decode().strip() == f"anymsg {version}"

    # npm publish --dry-run packs the built tarball without credentials. It does look the package up with GET (the
    # first run of this drill showed that), so the registry is a closed loopback port, then a recording one.
    home = tmp_path / "npm-home"
    home.mkdir()
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "npm_config_userconfig": str(home / ".npmrc"),
           "npm_config_globalconfig": str(home / "global.npmrc"), "npm_config_cache": str(home / "cache")}
    methods = []

    class Registry(BaseHTTPRequestHandler):
        def do_GET(self):
            methods.append(self.command)
            self.send_response(404)
            self.end_headers()

        def __getattr__(self, name):  # every other method is recorded too
            if name.startswith("do_"):
                return self.do_GET
            raise AttributeError(name)

        def log_message(self, format, *args):
            pass

    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        unreachable = f"http://127.0.0.1:{closed.getsockname()[1]}/"
    with ThreadingHTTPServer(("127.0.0.1", 0), Registry) as recording:
        threading.Thread(target=recording.serve_forever, daemon=True).start()
        try:
            for registry in (unreachable, f"http://127.0.0.1:{recording.server_port}/"):
                for tarball in (f"anymsg-{version}.tgz", f"deedkit-{version}.tgz"):
                    dry = run(["npm", "publish", str(dist / tarball), "--dry-run", "--ignore-scripts", "--registry", registry],
                              tmp_path, env, timeout=120)
                    assert dry.returncode == 0, dry.stderr
                    assert f"@{version}" in dry.stdout.decode(), tarball
        finally:
            recording.shutdown()
    assert methods and set(methods) == {"GET"}

    # The Tiden verdict passes on synthetic evidence of this commit and fails on one failed case.
    manifest, verdict = evidence_for(tmp_path, "passing", commit, {"python-1.xml": PASSING_CASE})
    assert verdict.returncode == 0, verdict.stdout + verdict.stderr
    assert manifest["sha"] == commit and manifest["ci_url"] == ATTEMPT_URL
    _, refused = evidence_for(tmp_path, "failing", commit, {"python-1.xml": FAILING_CASE})
    assert refused.returncode == 1 and refused.stdout.decode().startswith("The evidence records ")

    # The release record names the same commit; Tiden is a synthetic double, applied only around this last call.
    calls = release_cli(monkeypatch, [main_run(sha=commit)])
    main_push_manifest(tmp_path, sha=commit, ci_url=RUN_URL)
    assert run_release(monkeypatch, tmp_path, "--environment", "github-actions-synthetic") == 0
    create = next(call for call in calls if call[1:3] == ["release", "create"])
    assert create[create.index("--version") + 1] == "deedkit@" + commit
    assert create[create.index("--name") + 1] == "DeedKit " + commit[:12]
    assert json.loads((tmp_path / "tiden-release-receipt.json").read_text())["gateFromThisCommit"] is True
