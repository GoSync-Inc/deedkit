"""A test that hardens the process umask cannot change the modes of files later tests create (tests/conftest.py).

PR #1009 CI: a shard re-plan put tests/test_module_enroll.py, whose in-process production.main() sets umask 0o077,
before the sudo tests of tests/test_recordings_egress_check.py, whose root-created files then became unreadable.
The proof runs synthetic tests in a child pytest with the root conftest's fixture, under two incoming masks.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

CONFTEST = f'''import importlib.util
spec = importlib.util.spec_from_file_location("root_conftest", {str(ROOT / "tests" / "conftest.py")!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
if hasattr(module, "preserve_process_umask"):
    preserve_process_umask = module.preserve_process_umask
'''

TESTS = '''import os
import stat
import subprocess
import sys

import pytest

INCOMING = int(os.environ["INCOMING_UMASK"], 8)


def cli(outcome):
    os.umask(0o077)  # as a command-line main() hardens its own process
    if outcome == "exit":
        raise SystemExit(2)
    if outcome == "error":
        raise RuntimeError("synthetic")


def created_mode(path):
    subprocess.run([sys.executable, "-c", f"open({str(path)!r}, 'w').close()"], check=True)
    return stat.S_IMODE(path.stat().st_mode)


def test_1_the_mask_a_test_starts_with_is_the_incoming_one(tmp_path):
    assert created_mode(tmp_path / "first") == 0o666 & ~INCOMING


def test_2_a_cli_that_returns_keeps_its_hardened_mask_inside_the_test(tmp_path):
    cli("return")
    assert created_mode(tmp_path / "private") == 0o600


def test_3_a_cli_that_exits_keeps_its_hardened_mask_inside_the_test(tmp_path):
    with pytest.raises(SystemExit):
        cli("exit")
    assert created_mode(tmp_path / "private") == 0o600


@pytest.mark.xfail(raises=RuntimeError, strict=True)
def test_4_a_cli_error_that_fails_the_test():
    cli("error")


def test_5_a_later_subprocess_creates_files_with_the_incoming_mask(tmp_path):
    assert created_mode(tmp_path / "later") == 0o666 & ~INCOMING
'''


# spec: ANYM-624
@pytest.mark.parametrize("incoming", [0o002, 0o027], ids=["group-writable", "group-read-only"])
def test_a_hardened_umask_ends_with_its_test_and_later_tests_create_files_with_the_incoming_mask(tmp_path, incoming):
    (tmp_path / "conftest.py").write_text(CONFTEST)
    (tmp_path / "test_synthetic_umask.py").write_text(TESTS)
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"PYTEST_ADDOPTS", "DEEDKIT_TEST_SHARD", "PYTEST_PLUGINS"}}
    environment["INCOMING_UMASK"] = oct(incoming)
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:randomly",
                             "--rootdir", str(tmp_path), str(tmp_path)], cwd=tmp_path, env=environment,
                            capture_output=True, text=True, timeout=120, umask=incoming)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 passed, 1 xfailed" in result.stdout, result.stdout
