"""CI tool installs survive a GitHub releases outage: .github/scripts/fetch-release-asset.sh uses a cached
download, then the release URL, then the same asset through the GitHub API, and keeps a file only when its
SHA-256 equals the pin. A fake curl stands in for the network (outage 2026-11-08: releases answered 504)."""
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github/scripts/fetch-release-asset.sh"
URL = "https://github.com/example/tool/releases/download/v1.2.3/tool_linux_amd64.tar.gz"
GOOD = b"synthetic release asset\n"
PIN = hashlib.sha256(GOOD).hexdigest()
needs_tools = pytest.mark.skipif(not all(shutil.which(t) for t in ("bash", "jq", "sha256sum")),
                                 reason="the fetch script needs bash, jq and GNU sha256sum (CI runners have them)")

# A fake curl: logs every URL; MODE_<route> picks ok, bad (wrong bytes) or fail (HTTP 504) for the release
# download, the API release lookup and the API asset download.
FAKE_CURL = r"""#!/usr/bin/env bash
out= url=
while [ $# -gt 0 ]; do
  case $1 in -o) out=$2; shift;; -H|--connect-timeout|--max-time|--retry|--retry-max-time) shift;; https://*) url=$1;; esac
  shift
done
echo "$url" >> "$FAKE_LOG"
case $url in
  https://github.com/*/releases/download/*) route=download mode=$MODE_DOWNLOAD;;
  https://api.github.com/repos/example/tool/releases/tags/v1.2.3) route=lookup mode=$MODE_LOOKUP;;
  https://api.github.com/repos/example/tool/releases/assets/42) route=asset mode=$MODE_ASSET;;
  *) route=other mode=fail;;
esac
case $mode in
  fail) echo "curl: (22) The requested URL returned error: 504" >&2; exit 22;;
  bad) printf 'tampered\n' > "$out";;
  ok) if [ "$route" = lookup ]; then
        printf '{"assets":[{"name":"other.tar.gz","id":7},{"name":"tool_linux_amd64.tar.gz","id":42}]}' > "$out"
      else cp "$FAKE_GOOD" "$out"; fi;;
esac
"""


def _run(tmp_path, dest, pin=PIN, download="ok", lookup="ok", asset="ok"):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "curl").write_text(FAKE_CURL)
    (bin_dir / "curl").chmod(0o755)
    good = tmp_path / "good"
    good.write_bytes(GOOD)
    log = tmp_path / "curl.log"
    log.write_text("")
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_LOG": str(log),
           "FAKE_GOOD": str(good), "RUNNER_TEMP": str(tmp_path), "MODE_DOWNLOAD": download, "MODE_LOOKUP": lookup, "MODE_ASSET": asset}
    env.pop("GH_TOKEN", None)
    result = subprocess.run(["bash", str(SCRIPT), URL, pin, str(dest)], env=env, capture_output=True, text=True,
                            timeout=60)
    return result, log.read_text().split()


# spec: ANYM-137
@needs_tools
def test_a_releases_outage_falls_back_to_the_same_asset_through_the_api(tmp_path):
    dest = tmp_path / "tool.tar.gz"
    result, urls = _run(tmp_path, dest, download="fail")
    assert result.returncode == 0, result.stderr
    assert dest.read_bytes() == GOOD and not (tmp_path / "tool.tar.gz.part").exists()
    assert urls == [URL, "https://api.github.com/repos/example/tool/releases/tags/v1.2.3",
                    "https://api.github.com/repos/example/tool/releases/assets/42"]
    assert "GitHub API asset" in result.stdout
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("tool")) == ["tool.tar.gz"]


# spec: ANYM-137
@needs_tools
def test_after_one_failed_release_download_the_job_goes_straight_to_the_api(tmp_path):
    first, _ = _run(tmp_path, tmp_path / "tool.tar.gz", download="fail")
    second, urls = _run(tmp_path, tmp_path / "again.tar.gz", download="ok")
    assert first.returncode == 0 and second.returncode == 0, second.stderr
    assert URL not in urls and (tmp_path / "again.tar.gz").read_bytes() == GOOD


# spec: ANYM-137
@needs_tools
def test_a_cached_download_with_the_pinned_checksum_needs_no_network(tmp_path):
    dest = tmp_path / "tool.tar.gz"
    dest.write_bytes(GOOD)
    result, urls = _run(tmp_path, dest, download="fail", lookup="fail", asset="fail")
    assert result.returncode == 0, result.stderr
    assert urls == [] and dest.read_bytes() == GOOD


# spec: ANYM-137
@pytest.mark.parametrize("download, asset", [("bad", "bad"), ("fail", "bad"), ("bad", "fail"), ("fail", "fail")])
@needs_tools
def test_no_source_with_the_pinned_checksum_fails_and_keeps_nothing(tmp_path, download, asset):
    dest = tmp_path / "tool.tar.gz"
    dest.write_bytes(b"poisoned cache\n")
    result, _ = _run(tmp_path, dest, download=download, asset=asset)
    assert result.returncode == 1
    assert not dest.exists() and not (tmp_path / "tool.tar.gz.part").exists()


# spec: ANYM-137
@needs_tools
def test_a_tampered_release_download_is_discarded_for_the_api_copy(tmp_path):
    dest = tmp_path / "tool.tar.gz"
    result, _ = _run(tmp_path, dest, download="bad")
    assert result.returncode == 0 and dest.read_bytes() == GOOD
    assert "does not match the pinned SHA-256" in result.stderr


# spec: ANYM-137
@needs_tools
def test_only_a_github_release_url_and_a_sha256_are_accepted(tmp_path):
    result, urls = _run(tmp_path, tmp_path / "x", pin="0" * 63)
    assert result.returncode == 2 and urls == []
    env = {**os.environ}
    other = subprocess.run(["bash", str(SCRIPT), "https://example.com/tool.tar.gz", PIN, str(tmp_path / "x")],
                           env=env, capture_output=True, text=True, timeout=60)
    assert other.returncode == 2


# spec: ANYM-137
def test_every_release_download_in_ci_goes_through_the_cache_and_the_fetch_script():
    # Gitleaks and the security scanners; the Tiden CLI installs are not required checks (continue-on-error).
    for name, job, step in [("checks.yml", "verify", "Install the pinned gitleaks for the image-scan tests"),
                            ("images.yml", "build", "Install the pinned gitleaks"),
                            ("security.yml", "report", "Install pinned scanners")]:
        steps = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())["jobs"][job]["steps"]
        index = next(i for i, s in enumerate(steps) if s.get("name") == step)
        cache = steps[index - 1]
        assert cache["uses"].startswith("actions/cache@") and len(cache["uses"].split("@")[1].split()[0]) == 40
        run = steps[index]["run"]
        assert "fetch-release-asset.sh" in run and "sha256sum --check --strict" in run
        pins = re.findall(r"/releases/download/v([^/\s]+)/\S+\s*\\\s*\n\s*([0-9a-f]{64})", run)
        assert pins, name
        # The key names every tool's version and SHA-256, so a new pin never restores an old download.
        for version, sha in pins:
            assert f"-{version}-" in cache["with"]["key"] and sha in cache["with"]["key"], (name, version)
    assert os.access(SCRIPT, os.X_OK)
