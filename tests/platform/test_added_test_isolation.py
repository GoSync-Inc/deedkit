"""A synthetic proof that the files an autoloop pull request adds cannot turn an existing failing test green
(ANYM-578): each tamper tree masks a sentinel that fails by design when the suites run unfiltered, and the same
suites fail it once `scripts/added_tests.py strip` removed the listed files."""
import json
import os
import py_compile
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest

import scripts.added_tests as added_tests

# The existing tree: two Python suites whose sentinels fail by design and log that they ran.
EXISTING = {
    "pytest.ini": "[pytest]\n",
    "tests/checker.py": 'def compute():\n    return "actual"\n',
    "tests/test_sentinel.py": (
        "from pathlib import Path\n\n\n"
        "def test_sentinel():\n"
        '    with open(Path(__file__).parents[1] / "ran.log", "a") as log:\n'
        '        log.write("test_sentinel\\n")\n'
        "    from tests.checker import compute\n"
        '    assert compute() == "expected"  # fails by design\n'),
    "suite_b/expected.txt": "bad",
    "suite_b/test_reads.py": (
        "from pathlib import Path\n\n\n"
        "def test_reads():\n"
        "    root = Path(__file__).parents[1]\n"
        '    with open(root / "ran.log", "a") as log:\n'
        '        log.write("test_reads\\n")\n'
        '    assert (root / "suite_b/expected.txt").read_text() == "good"  # fails by design\n'),
}
GO_MODULE = {
    "gomod/go.mod": "module example.com/sentinel\n\ngo 1.21\n",
    "gomod/calc.go": "package calc\n\nvar offset = 1\n\nfunc Add(a, b int) int { return a + b + offset }\n",
    "gomod/calc_test.go": ('package calc\n\nimport "testing"\n\n'
                           "func TestSentinel(t *testing.T) {\n"
                           "\tif Add(1, 1) != 2 { // fails by design\n\t\tt.Fatal(Add(1, 1))\n\t}\n}\n"),
}
MASK_PLUGIN = ("import pytest\n\n\n"
               "@pytest.hookimpl(hookwrapper=True)\n"
               "def pytest_runtest_makereport(item, call):\n"
               "    outcome = yield\n"
               "    report = outcome.get_result()\n"
               '    report.outcome, report.longrepr = "passed", None\n')
PYC_SOURCE = ("import sys, types\n"
              'fake = types.ModuleType("tests.checker")\n'
              'fake.compute = lambda: "expected"\n'
              'sys.modules["tests.checker"] = fake\n'
              "checker = fake\n")
# Each vector: the files the pull request adds, the existing files it needs beside them and the sentinel it masks.
VECTORS = {
    "runner-patch": ({"tests/test_zz_patch.py": "from _pytest.python import Function\n\nFunction.runtest = lambda self: None\n"},
                     {}, "test_sentinel"),
    "pytest-plugins": ({"tests/test_aa_plugins.py": 'pytest_plugins = ["mask_plugin"]\n', "tests/mask_plugin.py": MASK_PLUGIN},
                       {}, "test_sentinel"),
    "sourceless-pyc": ({"tests/__init__.pyc": None}, {}, "test_sentinel"),
    "go-init-beside-package": ({"gomod/zz_test.go": "package calc\n\nfunc init() { offset = 0 }\n"}, GO_MODULE, "TestSentinel"),
    "rewrites-a-later-suites-file": ({"tests/test_aa_rewrite.py": (
        "from pathlib import Path\n\n\n"
        "def test_rewrite():\n"
        '    (Path(__file__).parents[1] / "suite_b/expected.txt").write_text("good")\n')}, {}, "test_reads"),
}


def write(root, files):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if text is None:  # a sourceless bytecode package: compiled here for this interpreter
            source = root.parent / "pyc-source.py"
            source.write_text(PYC_SOURCE)
            py_compile.compile(str(source), cfile=str(path), doraise=True)
        else:
            path.write_text(text)


def environment():
    env = {key: value for key, value in os.environ.items() if not key.startswith(("PYTEST", "PYTHONPATH"))}
    return {**env, "PYTHONDONTWRITEBYTECODE": "1", "GOTOOLCHAIN": "local", "GOFLAGS": "-mod=mod", "GOWORK": "off"}


def outcomes(root, reports):
    """Run the existing suites the way the required jobs do, one process each, and read each test's outcome."""
    results = {}
    for suite in ("tests", "suite_b"):
        junit = reports / f"{suite}.xml"
        subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={junit}", suite],
                       cwd=root, env=environment(), capture_output=True, timeout=180, check=False)
        for case in ET.parse(junit).iter("testcase"):
            failed = case.find("failure") is not None or case.find("error") is not None
            results[case.get("name")] = "failed" if failed else "skipped" if case.find("skipped") is not None else "passed"
    if (root / "gomod").is_dir():
        go = subprocess.run(["go", "test", "-count=1", "-v", "./..."], cwd=root / "gomod", env=environment(),
                            capture_output=True, text=True, timeout=300, check=False)
        for line in go.stdout.splitlines():
            if line.startswith(("--- PASS: ", "--- FAIL: ")):
                results[line.split()[2]] = "passed" if line.startswith("--- PASS") else "failed"
    ran = (root / "ran.log").read_text().split() if (root / "ran.log").exists() else []
    return results, ran


# spec: ANYM-578
@pytest.mark.parametrize("vector", list(VECTORS))
def test_an_added_file_masks_a_failing_sentinel_unfiltered_and_the_filtered_suites_fail_it(tmp_path, vector):
    added, existing, sentinel = VECTORS[vector]
    if existing is GO_MODULE and shutil.which("go") is None:
        if os.environ.get("CI"):
            pytest.fail("CI runs the Go vector: no `go` on this runner")
        pytest.skip("no `go` outside CI")
    unfiltered, filtered = tmp_path / "unfiltered" / "tree", tmp_path / "filtered" / "tree"
    for root in (unfiltered, filtered):
        write(root, {**EXISTING, **existing})
        write(root, added)
    # The inventory the pull request's comparison yields lists every added file, whatever its name or directory.
    document = {"files": sorted(added_tests.listed([{"filename": name, "status": "added"} for name in added])), "provenance": []}
    assert document["files"] == sorted(added)
    added_tests.strip(added_tests.inventory(json.dumps(document)), [filtered], tmp_path / "evidence")
    assert not any((filtered / name).exists() for name in added)

    masked, _ = outcomes(unfiltered, tmp_path / "unfiltered")
    assert masked[sentinel] == "passed", "the tamper tree no longer masks the sentinel: the proof proves nothing"
    result, ran = outcomes(filtered, tmp_path / "filtered")
    assert result[sentinel] == "failed"
    if sentinel != "TestSentinel":
        assert sentinel in ran  # the sentinel executed and failed, it was not skipped or uncollected


@pytest.fixture
def uv_shim(tmp_path, monkeypatch):
    """`uv run --frozen --group dev pytest ...` as this interpreter's pytest: the real process, without a project lock."""
    shim = tmp_path / "bin" / "uv"
    shim.parent.mkdir()
    shim.write_text('#!/bin/sh\nwhile [ "$#" -gt 0 ] && [ "$1" != pytest ]; do shift; done\nshift\n'
                    'exec "$ADDED_TESTS_PYTHON" -m pytest "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("ADDED_TESTS_PYTHON", sys.executable)
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.delenv("PYTEST_ADDOPTS", raising=False)


# spec: ANYM-578
@pytest.mark.parametrize("body, returncode, result", [
    ('    (Path(__file__).parents[1] / "ran.log").write_text("added")\n', 0, "passed"),
    ('    (Path(__file__).parents[1] / "ran.log").write_text("added")\n    assert False\n', 1, "failed"),
    (None, 0, "none"),
], ids=["passing", "failing", "empty"])
def test_the_added_test_job_runs_the_added_tests_alone_and_records_their_result(tmp_path, uv_shim, body, returncode, result):
    root = tmp_path / "tree"
    write(root, {"pytest.ini": "[pytest]\n", "tests/test_sentinel.py": EXISTING["tests/test_sentinel.py"]})
    files = []
    if body is not None:
        write(root, {"tests/test_added.py": "from pathlib import Path\n\n\ndef test_added():\n" + body})
        files = ["tests/test_added.py"]
    assert added_tests.run({"files": files, "provenance": []}, root, tmp_path / "evidence") == returncode
    report = json.loads((tmp_path / "evidence/added.json").read_text())
    assert report["result"] == result
    assert json.loads((tmp_path / "evidence/outcomes.json").read_text()) == {"module_checks": {}}
    # Only the added test ran: the existing failing sentinel never did.
    assert (root / "ran.log").read_text() == "added" if body is not None else not (root / "ran.log").exists()
    if body is not None:
        assert report["runs"][0]["argv"][-1] == "tests/test_added.py" and report["runs"][0]["returncode"] == returncode
