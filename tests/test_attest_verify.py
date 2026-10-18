"""The tokenless attestation verifier: its Go tests pass and the pinned build reproduces the recorded SHA-256."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess

import pytest

HELPER = Path(__file__).resolve().parents[1] / "tools" / "attest-verify"
BUILD = {"GOTOOLCHAIN": "go1.26.9", "CGO_ENABLED": "0", "GOOS": "linux", "GOARCH": "amd64", "GOFLAGS": ""}


def go() -> str:
    found = shutil.which("go")
    if not found:
        (pytest.fail if os.environ.get("CI") else pytest.skip)("go is not on PATH; CI installs Go 1.26.9 before pytest")
        raise AssertionError("unreachable")
    return found


def test_attest_verify_go_tests_pass():
    result = subprocess.run([go(), "test", "./..."], cwd=HELPER, env={**os.environ, "GOFLAGS": ""}, capture_output=True, text=True, timeout=900)
    assert result.returncode == 0, result.stdout + result.stderr


def test_attest_verify_reproducible_build_matches_the_recorded_sha256(tmp_path):
    out = tmp_path / "attest-verify"
    result = subprocess.run([go(), "build", "-trimpath", "-buildvcs=false", "-ldflags=-buildid=", "-o", str(out), "."],
        cwd=HELPER, env={**os.environ, **BUILD}, capture_output=True, text=True, timeout=900)
    assert result.returncode == 0, result.stdout + result.stderr
    recorded = (HELPER / "attest-verify.linux-amd64.sha256").read_text()
    assert recorded == f"{hashlib.sha256(out.read_bytes()).hexdigest()}  attest-verify\n"
