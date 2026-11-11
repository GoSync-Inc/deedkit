"""The CI apt helper installs through the first Ubuntu mirror that answers, and every workflow apt step uses it
(outage 2026-11-11: every Canonical host was unreachable from the runners, so a retry on archive.ubuntu.com
could not help)."""
import os
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github/scripts/apt-install.sh"
HELPER_IN_STEP = "$GITHUB_WORKSPACE/ci-tools/.github/scripts/apt-install.sh"
IMAGE_MIRRORS = "http://azure.archive.ubuntu.com/ubuntu/\tpriority:1\nhttps://archive.ubuntu.com/ubuntu/\tpriority:2\nhttps://security.ubuntu.com/ubuntu/\tpriority:3\n"
DEB822 = "Types: deb\nURIs: mirror+file:/etc/apt/apt-mirrors.txt\nSuites: noble noble-updates\nComponents: main\n"
DIRECT_DEB822 = "Types: deb\nURIs: http://azure.archive.ubuntu.com/ubuntu\nSuites: noble noble-updates\nComponents: main\n\nTypes: deb\nURIs: http://security.ubuntu.com/ubuntu\nSuites: noble-security\nComponents: main\n"
MICROSOFT = "Types: deb\nURIs: https://packages.microsoft.com/ubuntu/24.04/prod\nSuites: noble\nComponents: main\n"
# Stand-ins for what a runner provides; each logs its call, the rest of the script runs for real.
FAKES = {
    "sudo": 'exec "$@"\n',
    "timeout": 'shift 2\nexec "$@"\n',
    "curl": '''for url; do :; done
host="${url#*://}"; host="${host%%/*}"
echo "curl $url" >> "$FAKE_LOG"
case " $FAKE_UP " in *" $host "*) exit 0;; esac
exit 22
''',
    "apt-get": '''if [ -f "$APT_ETC/apt-mirrors.txt" ]; then mirror="$(awk 'NF {print $1; exit}' "$APT_ETC/apt-mirrors.txt")"
else mirror="$(grep -h '^URIs:' "$APT_ETC/sources.list.d/ubuntu.sources" | head -n 1)"; fi
case "$mirror" in *"://"*) host="${mirror#*://}"; host="${host%%/*}";; *) host=unknown;; esac
echo "apt-get $* @ $host" >> "$FAKE_LOG"
case "$*" in *" update"*) case " $FAKE_APT_FAIL " in *" $host "*) exit 100;; esac;; esac
exit 0
''',
}


def run_helper(tmp_path, up, apt_fail=(), mirror_list: str | None = IMAGE_MIRRORS, sources=DEB822, packages=("ffmpeg", "age"), **env):
    etc = tmp_path / "apt"
    (etc / "sources.list.d").mkdir(parents=True)
    (etc / "sources.list.d/ubuntu.sources").write_text(sources)
    (etc / "sources.list.d/microsoft-prod.sources").write_text(MICROSOFT)
    if mirror_list is not None:
        (etc / "apt-mirrors.txt").write_text(mirror_list)
    (tmp_path / "os-release").write_text('NAME="Ubuntu"\nVERSION_CODENAME=noble\n')
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name, body in FAKES.items():
        (fake_bin / name).write_text("#!/bin/sh\n" + body)
        (fake_bin / name).chmod(0o755)
    log = tmp_path / "calls.log"
    log.touch()
    proc = subprocess.run(
        ["bash", str(SCRIPT), *packages], capture_output=True, text=True, timeout=60,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}", "APT_ETC": str(etc), "APT_OS_RELEASE": str(tmp_path / "os-release"),
             "FAKE_LOG": str(log), "FAKE_UP": " ".join(up), "FAKE_APT_FAIL": " ".join(apt_fail), **env})
    return proc, log.read_text().splitlines(), etc


# spec: ANYM-500
def test_the_image_mirror_list_is_left_alone_while_its_first_mirror_answers(tmp_path):
    proc, log, etc = run_helper(tmp_path, up=["azure.archive.ubuntu.com"])
    assert proc.returncode == 0, proc.stderr
    assert (etc / "apt-mirrors.txt").read_text() == IMAGE_MIRRORS
    # One probe, of the pocket a partial mirror lacks first, then update and install of exactly the named packages.
    assert log == ["curl http://azure.archive.ubuntu.com/ubuntu/dists/noble-security/InRelease",
                   "apt-get -o Acquire::http::Timeout=20 -o Acquire::https::Timeout=20 -o Acquire::Retries=3 update @ azure.archive.ubuntu.com",
                   "apt-get -o Acquire::http::Timeout=20 -o Acquire::https::Timeout=20 -o Acquire::Retries=3 install -y ffmpeg age @ azure.archive.ubuntu.com"]


# spec: ANYM-500
def test_every_canonical_host_down_installs_through_a_mirror_outside_canonical(tmp_path):
    # The 2026-11-11 outage: azure, archive and security.ubuntu.com all refused the runners; university mirrors answered.
    proc, log, etc = run_helper(tmp_path, up=["mirrors.edge.kernel.org"])
    assert proc.returncode == 0, proc.stderr
    assert (etc / "apt-mirrors.txt").read_text() == "http://mirrors.edge.kernel.org/ubuntu/\n"
    applied = [line for line in log if line.startswith("apt-get")]
    assert [line.rsplit(" @ ", 1)[1] for line in applied] == ["mirrors.edge.kernel.org"] * 2
    assert "install -y ffmpeg age" in applied[1]
    assert "does not answer" in proc.stdout and "azure.archive.ubuntu.com" in proc.stdout


# spec: ANYM-500
def test_a_mirror_that_answers_but_fails_apt_moves_on_to_the_next_one(tmp_path):
    proc, log, etc = run_helper(tmp_path, up=["azure.archive.ubuntu.com", "archive.ubuntu.com", "mirrors.mit.edu"],
                                apt_fail=["azure.archive.ubuntu.com"])
    assert proc.returncode == 0, proc.stderr
    hosts = [line.rsplit(" @ ", 1)[1] for line in log if line.startswith("apt-get")]
    assert hosts == ["azure.archive.ubuntu.com", "archive.ubuntu.com", "archive.ubuntu.com"]
    assert (etc / "apt-mirrors.txt").read_text() == "http://archive.ubuntu.com/ubuntu/\n"
    assert "attempt 1 on http://azure.archive.ubuntu.com/ubuntu failed" in proc.stdout


# spec: ANYM-500
def test_no_answering_mirror_fails_without_running_apt(tmp_path):
    proc, log, _ = run_helper(tmp_path, up=[])
    assert proc.returncode == 1
    assert not [line for line in log if line.startswith("apt-get")]
    assert "no Ubuntu mirror installed ffmpeg age" in proc.stderr


# spec: ANYM-500
def test_attempts_are_capped_so_the_step_timeout_is_not_reached_first(tmp_path):
    everywhere = ["azure.archive.ubuntu.com", "archive.ubuntu.com", "mirrors.edge.kernel.org", "mirrors.mit.edu", "mirror.arizona.edu"]
    updates = lambda log: [line for line in log if line.startswith("apt-get") and " update " in line]
    (tmp_path / "default").mkdir()
    proc, log, _ = run_helper(tmp_path / "default", up=everywhere, apt_fail=everywhere)
    assert proc.returncode == 1 and len(updates(log)) == 3
    (tmp_path / "two").mkdir()
    proc, log, _ = run_helper(tmp_path / "two", up=everywhere, apt_fail=everywhere, APT_MAX_ATTEMPTS="2")
    assert proc.returncode == 1 and len(updates(log)) == 2


# spec: ANYM-500
def test_an_image_without_a_mirror_list_has_its_ubuntu_source_files_rewritten(tmp_path):
    sources_list = "deb [arch=amd64] http://security.ubuntu.com/ubuntu noble-security main\ndeb-src http://azure.archive.ubuntu.com/ubuntu noble main\n"
    proc, log, etc = run_helper(tmp_path, up=["mirrors.edge.kernel.org"], mirror_list=None, sources=DIRECT_DEB822)
    assert proc.returncode == 0, proc.stderr
    # The probe of the image's own first mirror names the host the source file names, not a guessed one.
    assert log[0] == "curl http://azure.archive.ubuntu.com/ubuntu/dists/noble-security/InRelease"
    rewritten = (etc / "sources.list.d/ubuntu.sources").read_text()
    assert rewritten.count("URIs: http://mirrors.edge.kernel.org/ubuntu/\n") == 2 and "ubuntu.com" not in rewritten
    assert "Suites: noble-security" in rewritten
    assert (etc / "sources.list.d/microsoft-prod.sources").read_text() == MICROSOFT
    assert not (etc / "apt-mirrors.txt").exists()
    # The one-line list format is rewritten the same way, options kept.
    (etc / "sources.list").write_text(sources_list)
    proc = subprocess.run(["bash", str(SCRIPT), "age"], capture_output=True, text=True, timeout=60,
                          env={**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", "APT_ETC": str(etc),
                               "APT_OS_RELEASE": str(tmp_path / "os-release"), "FAKE_LOG": str(tmp_path / "calls.log"), "FAKE_UP": "mirrors.mit.edu", "FAKE_APT_FAIL": ""})
    assert proc.returncode == 0, proc.stderr
    assert (etc / "sources.list").read_text() == "deb [arch=amd64] http://mirrors.mit.edu/ubuntu/ noble-security main\ndeb-src http://mirrors.mit.edu/ubuntu/ noble main\n"


# spec: ANYM-500
def test_the_helper_needs_a_package_and_a_codename(tmp_path):
    assert subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True).returncode == 2
    (tmp_path / "os-release").write_text("NAME=Ubuntu\n")
    proc = subprocess.run(["bash", str(SCRIPT), "age"], capture_output=True, text=True, env={**os.environ, "APT_OS_RELEASE": str(tmp_path / "os-release")})
    assert proc.returncode == 2 and "VERSION_CODENAME" in proc.stderr


# spec: ANYM-500
def test_every_workflow_apt_step_goes_through_the_mirror_helper():
    installs = []
    for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
        for job_name, job in yaml.safe_load(path.read_text())["jobs"].items():
            checked_out = False
            for step in job.get("steps", ()):
                checked_out = checked_out or step.get("with", {}).get("path") == "ci-tools"
                run = step.get("run", "")
                where = (path.name, job_name, step.get("name"))
                assert "apt-get" not in run and "apt install" not in run, where
                if "apt-install.sh" in run:
                    # The workflow commit's own copy: the tested source may be a baseline without the script.
                    assert checked_out and HELPER_IN_STEP in run, where
                    installs.append((job_name, step["name"]))
    # The python shards share verify's step through a YAML anchor, so each of them appears here too.
    assert {("verify", "Native extraction tools"), ("services", "Android device service tests and installable package")} <= set(installs)
    assert [job for job, name in installs if job.startswith("python")] and all(name == "Native extraction tools" for job, name in installs if job != "services")


# spec: ANYM-500
@pytest.mark.parametrize("path", sorted((ROOT / ".github/scripts").glob("*.sh")), ids=lambda path: path.name)
def test_ci_shell_helpers_are_valid_bash_and_executable(path):
    assert subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True).returncode == 0
    assert os.access(path, os.X_OK)
