"""The root Python suite runs in file-level shards (seven in CI) on parallel runners; synthetic workflows and reports only."""
import fnmatch
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys

import pytest
import xml.etree.ElementTree as ET
import yaml

from scripts import python_shards, tiden_ci
from tests.test_ci_reuse import go_stream
from tests.test_tiden_ci import ATTEMPT_URL, PASSED, assembled_rows, evidence_fragments, shard_report

ROOT = Path(__file__).resolve().parents[1]
WEIGHTS = json.loads(python_shards.WEIGHTS.read_text())


def jobs():
    return yaml.safe_load((ROOT / ".github/workflows/checks.yml").read_text())["jobs"]


def test_every_test_module_runs_in_exactly_one_of_the_four_shards():
    modules = python_shards.modules(ROOT)
    assert "tests/test_python_shards.py" in modules and "tests/platform/test_stack.py" in modules
    assert all(name.startswith("tests/") and any(fnmatch.fnmatch(Path(name).name, pattern) for pattern in ("test_*.py", "*_test.py"))
               for name in modules)
    owners = {name: [index for index in range(1, 5) if not python_shards.ignored(ROOT / name, (index, 4))] for name in modules}
    assert all(len(shards) == 1 for shards in owners.values()), {k: v for k, v in owners.items() if len(v) != 1}
    assert {shards[0] for shards in owners.values()} == {1, 2, 3, 4}
    # Without a shard the whole suite runs, as on a developer machine.
    assert not any(python_shards.ignored(ROOT / name, None) for name in modules)


def test_the_plan_is_deterministic_balanced_and_gives_shard_one_less_work():
    files = [f"tests/test_{index}.py" for index in range(40)]
    weights = {"default": 3, "head_start": 20, "seconds": {"tests/test_0.py": 50, "tests/test_1.py": 40}}
    plan = python_shards.plan(files, 4, weights)
    assert set(plan) == set(files) and set(plan.values()) == {1, 2, 3, 4}
    shuffled = files[:]
    random.Random(7).shuffle(shuffled)
    assert python_shards.plan(shuffled, 4, weights) == plan  # the input order never changes the plan
    load = {index: sum(weights["seconds"].get(name, 3) for name, owner in plan.items() if owner == index) for index in range(1, 5)}
    load[1] += weights["head_start"]
    assert max(load.values()) - min(load.values()) <= 50  # no shard exceeds another by more than its heaviest module
    assert plan["tests/test_0.py"] != 1 and plan["tests/test_0.py"] != plan["tests/test_1.py"]
    # One shard runs everything, without a head start.
    assert set(python_shards.plan(files, 1, weights).values()) == {1}
    # The committed plan balances the real suite: verify's shard plus its head start is within a minute of the others.
    real = python_shards.plan(python_shards.modules(ROOT), tiden_ci.PYTHON_SHARDS, WEIGHTS)
    seconds = [sum(WEIGHTS["seconds"].get(name, WEIGHTS["default"]) for name, owner in real.items() if owner == index)
               for index in range(1, tiden_ci.PYTHON_SHARDS + 1)]
    seconds[0] += WEIGHTS["head_start"]
    assert max(seconds) - min(seconds) < 60, seconds


# spec: ANYM-624
def test_the_committed_plan_balances_the_measured_suite_and_job_overheads():
    # Ten full CI runs of 2026-10-29: each module's median seconds and each job's median time outside its pytest
    # step (verify builds and runs the other checks). The committed weights and head start must split that measured
    # work so that no shard job ends more than 10 % after the mean one.
    measured = json.loads((ROOT / "tests/fixtures/python-shards-measured-2026-10-29.json").read_text())
    plan = python_shards.plan(sorted(measured["seconds"]), tiden_ci.PYTHON_SHARDS, WEIGHTS)
    ends = [measured["overhead"]["verify" if index == 1 else "python"]
            + sum(seconds for name, seconds in measured["seconds"].items() if plan[name] == index)
            for index in range(1, tiden_ci.PYTHON_SHARDS + 1)]
    assert max(ends) <= 1.10 * sum(ends) / len(ends), ends


# spec: ANYM-624
def test_weights_take_each_modules_median_over_the_runs_and_the_measured_head_start(tmp_path, monkeypatch):
    def report(path, **seconds):
        cases = "".join(f'<testcase classname="c" name="t{index}" file="tests/{name}.py" time="{value}"/>'
                        for index, (name, value) in enumerate(seconds.items()))
        path.write_text(f'<testsuites><testsuite name="pytest" tests="{len(seconds)}">{cases}</testsuite></testsuites>')
        return path
    # Three runs; test_b ran in two of them only, and one run's report is split by shard.
    runs = [report(tmp_path / "1.xml", test_a=10, test_b=2), report(tmp_path / "2a.xml", test_a=30),
            report(tmp_path / "2b.xml", test_b=4), report(tmp_path / "3.xml", test_a=20)]
    assert python_shards.median_seconds(runs) == {"tests/test_a.py": 20, "tests/test_b.py": 3}
    weights = tmp_path / "python_shards.json"
    weights.write_text(json.dumps({"default": 3, "head_start": 220, "seconds": {"tests/test_old.py": 1}}))
    monkeypatch.setattr(python_shards, "WEIGHTS", weights)
    monkeypatch.setattr(sys, "argv", ["python_shards.py", "weights", *map(str, runs), "--head-start", "292.6"])
    assert python_shards.main() == 0
    assert json.loads(weights.read_text()) == {"default": 3, "head_start": 293,
                                               "seconds": {"tests/test_a.py": 20, "tests/test_b.py": 3}}


def test_a_module_without_a_timing_still_runs_in_exactly_one_shard(tmp_path):
    (tmp_path / "tests" / "nested").mkdir(parents=True)
    names = ["tests/test_new.py", "tests/nested/test_deep.py", *(f"tests/test_{index}.py" for index in range(6))]
    for name in names:
        (tmp_path / name).write_text("")
    (tmp_path / "tests" / "helpers.py").write_text("")
    assert python_shards.modules(tmp_path) == sorted(names)
    python_shards._plan.cache_clear()
    try:
        owners = {name: [index for index in range(1, 5) if not python_shards.ignored(tmp_path / name, (index, 4), tmp_path)]
                  for name in names}
        assert all(len(shards) == 1 for shards in owners.values()), owners
        # Directories, files outside the plan and files outside the checkout are never left out by any shard.
        for path in (tmp_path / "tests", tmp_path / "tests" / "helpers.py", ROOT / "tests" / "conftest.py", Path("/etc/hosts")):
            assert not any(python_shards.ignored(path, (index, 4), tmp_path) for index in range(1, 5)), path
    finally:
        python_shards._plan.cache_clear()


@pytest.mark.parametrize("value", ["0/4", "5/4", "1/0", "2", "a/4", "1/4/2", " 1/4"])
def test_a_malformed_shard_is_an_error_never_the_whole_suite_or_nothing(value):
    with pytest.raises(ValueError, match="DEEDKIT_TEST_SHARD"):
        python_shards.selected(value)
    assert python_shards.selected(None) is None and python_shards.selected("") is None
    assert python_shards.selected("3/4") == (3, 4)


def shard_plugin(tmp_path):
    """The selector as CI runs it: the workflow commit's two files, copied to a directory of their own."""
    plugin = tmp_path / "shard-plugin"
    plugin.mkdir()
    for name in ("python_shards.py", "python_shards.json"):
        shutil.copy(ROOT / "scripts" / name, plugin / name)
    return plugin / "python_shards.py"


def collected(directory, shard=None, *, plugin, cwd=ROOT):
    env = {key: value for key, value in os.environ.items() if key != "DEEDKIT_TEST_SHARD"}
    if shard:
        env["DEEDKIT_TEST_SHARD"] = shard
    result = subprocess.run([sys.executable, str(plugin), "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", directory],
                            cwd=cwd, env=env, capture_output=True, text=True, timeout=300)
    return result.returncode, sorted(line for line in result.stdout.splitlines() if line.startswith("tests/"))


def test_pytest_collects_each_module_in_exactly_the_shard_the_plan_names(tmp_path):
    # The workflow commit's selector applies the shard while pytest collects; the four shards are the full collection.
    plugin = shard_plugin(tmp_path)
    code, full = collected("tests/platform", plugin=plugin)
    assert code == 0 and len(full) >= 5
    shards = [collected("tests/platform", f"{index}/4", plugin=plugin) for index in range(1, 5)]
    union = [line for code, lines in shards for line in lines]
    assert sorted(union) == full and len(set(union)) == len(union)
    plan = python_shards.plan(python_shards.modules(ROOT), 4, WEIGHTS)
    for index, (code, lines) in enumerate(shards, 1):
        assert code in (0, 5)  # 5: this shard owns no module of the directory
        assert all(plan[line.split(":")[0]] == index for line in lines)
    code, _ = collected("tests/platform", "5/4", plugin=plugin)
    assert code == 4  # a usage error, so CI never silently runs another shard's modules


def synthetic_checkout(root, *, hook=False):
    """A checkout with ordinary modules, a module of pytest's `*_test.py` pattern and a symlinked module."""
    (root / "tests").mkdir(parents=True)
    (root / "elsewhere").mkdir()
    (root / "pytest.ini").write_text("[pytest]\n")
    for index in range(8):
        (root / "tests" / f"test_{index}.py").write_text(f"def test_{index}():\n    pass\n")
    (root / "tests" / "suffix_test.py").write_text("def test_suffix():\n    pass\n")
    (root / "elsewhere" / "test_target.py").write_text("def test_link():\n    pass\n")
    (root / "tests" / "test_link.py").symlink_to(Path("..") / "elsewhere" / "test_target.py")
    if hook:  # an older revision's own hook, which the workflow commit's selector does not need
        (root / "tests" / "conftest.py").write_text("def pytest_ignore_collect(collection_path, config):\n    return None\n")
    return root


@pytest.mark.parametrize("hook", [False, True], ids=["baseline-without-a-hook", "own-hook"])
def test_every_collected_module_of_any_checkout_runs_in_exactly_one_shard(tmp_path, hook):
    # Neither pytest's second default pattern, a symlinked module nor a baseline source without a shard hook of
    # its own lets a module run in every shard: the selector comes from the workflow commit, not the checkout.
    checkout = synthetic_checkout(tmp_path / "checkout", hook=hook)
    plugin = shard_plugin(tmp_path)
    code, full = collected("tests", plugin=plugin, cwd=checkout)
    assert code == 0 and "tests/suffix_test.py::test_suffix" in full and "tests/test_link.py::test_link" in full
    assert len(full) == 10
    shards = [collected("tests", f"{index}/4", plugin=plugin, cwd=checkout) for index in range(1, 5)]
    union = [line for code, lines in shards for line in lines]
    assert all(code in (0, 5) for code, _ in shards)
    assert sorted(union) == full and len(set(union)) == len(union)


def test_a_collected_module_outside_the_plan_is_a_collection_error(tmp_path):
    # A directory symlink takes pytest to modules the plan cannot name: an error, never a module run everywhere.
    checkout = synthetic_checkout(tmp_path / "checkout")
    (checkout / "outside").mkdir()
    (checkout / "outside" / "test_far.py").write_text("def test_far():\n    pass\n")
    (checkout / "tests" / "linked").symlink_to(Path("..") / "outside", target_is_directory=True)
    plugin = shard_plugin(tmp_path)
    code, full = collected("tests", plugin=plugin, cwd=checkout)
    assert code == 0 and "tests/linked/test_far.py::test_far" in full
    env = {**os.environ, "DEEDKIT_TEST_SHARD": "2/4"}
    result = subprocess.run([sys.executable, str(plugin), "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "tests"],
                            cwd=checkout, env=env, capture_output=True, text=True, timeout=300)
    assert result.returncode != 0 and "outside the shard plan" in result.stdout + result.stderr


def test_a_python_files_pattern_with_a_directory_is_a_usage_error_in_a_sharded_run(tmp_path):
    # pytest matches such a pattern against the whole path, which the plan's file-name match cannot follow.
    checkout = synthetic_checkout(tmp_path / "checkout")
    (checkout / "pytest.ini").write_text("[pytest]\npython_files = tests/test_*.py tests/*_test.py\n")
    plugin = shard_plugin(tmp_path)
    code, full = collected("tests", plugin=plugin, cwd=checkout)
    assert code == 0 and len(full) == 10  # the whole suite, unsharded, as on a developer machine
    env = {**os.environ, "DEEDKIT_TEST_SHARD": "1/4"}
    result = subprocess.run([sys.executable, str(plugin), "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "tests"],
                            cwd=checkout, env=env, capture_output=True, text=True, timeout=300)
    assert result.returncode == 4 and "file names only" in result.stdout + result.stderr


def test_python_shard_jobs_run_verifys_suite_step_on_runners_of_their_own():
    all_jobs = jobs()
    verify, python = all_jobs["verify"], all_jobs["python-2"]
    # One job per shard, so each has its own completed result; the jobs differ only in their shard.
    for job in tiden_ci.SHARD_JOBS:
        assert all_jobs[job]["strategy"] == {"matrix": {"shard": [int(job.split("-")[1])]}}
        assert {key: value for key, value in all_jobs[job].items() if key != "strategy"} == {
            key: value for key, value in python.items() if key != "strategy"}
    assert "python" not in all_jobs
    assert python["name"] == f"python (${{{{ matrix.shard }}}}/{tiden_ci.PYTHON_SHARDS})"
    # Skipped exactly when verify is, never after it, and with verify's PostgreSQL, images and pins.
    assert python["needs"] == verify["needs"] == "documentation" and python["if"] == verify["if"]
    assert python["services"] == verify["services"] and python["env"] == verify["env"]
    assert python["outputs"] == verify["outputs"] == {"source_sha": "${{ steps.source.outputs.sha }}"}
    assert python["timeout-minutes"] <= verify["timeout-minutes"]
    named = {step.get("name") or step["uses"]: step for step in python["steps"]}
    ours = {step.get("name") or step["uses"]: step for step in verify["steps"]}
    for name in ("Start evidence record", "Start the release router image build and the Python image pull in the background",
                 "Native extraction tools", "Install pinned dependencies", "Build native WhatsApp offline normalizer",
                 "Install the pinned gitleaks for the image-scan tests", "Wait for the background router image and Python image"):
        assert named[name]["run"] == ours[name]["run"], name
    for uses in (step["uses"] for step in verify["steps"] if "uses" in step and "upload-artifact" not in step["uses"]):
        assert uses in named, uses
    # The same suite command; only the shard and the step that names the built version differ.
    suite = next(step for step in python["steps"] if step.get("id") == "python")
    theirs = next(step for step in verify["steps"] if step.get("id") == "python")
    assert suite["name"] == theirs["name"] and suite["run"] == theirs["run"]
    assert theirs["env"]["DEEDKIT_TEST_SHARD"] == f"1/{tiden_ci.PYTHON_SHARDS}"
    assert suite["env"] == {**theirs["env"], "DEEDKIT_TEST_SHARD": f"${{{{ matrix.shard }}}}/{tiden_ci.PYTHON_SHARDS}",
                            **{key: theirs["env"][key].replace("steps.build.", "steps.package.") for key in ("ANYMSG_TEST_WHEEL", "ANYMSG_TEST_SDIST")}}
    assert '--junitxml="$RUNNER_TEMP/ci-evidence/python-${DEEDKIT_TEST_SHARD%%/*}.xml"' in suite["run"]
    package = next(step for step in python["steps"] if step.get("id") == "package")
    assert "uv build --project services/messaging --out-dir dist" in package["run"]
    assert package["run"].split("uv build")[0] == next(s for s in verify["steps"] if s.get("id") == "build")["run"].split("uv build")[0]
    venvs = named["Gateway and calendar environments the root suite starts"]["run"]
    assert "(cd services/gateway && uv sync --locked --group dev)" in venvs and "(cd services/calendar && uv sync --locked --group dev)" in venvs
    # A shard's only check is `python`; its fragment carries its outcome and report under its own name.
    ids = {step.get("id") for step in python["steps"]} - {None}
    assert ids == {"source", "package", "python"} and not ids & (set(tiden_ci.CHECKS) - {"python"})
    # Both run the suite through the workflow commit's selector, checked out as ci-tools beside the tested source.
    for job in (verify, python):
        assert {"persist-credentials": False, "ref": "${{ github.sha }}", "path": "ci-tools"} in [step.get("with") for step in job["steps"]]
    assert 'cp ci-tools/scripts/python_shards.py ci-tools/scripts/python_shards.json "$RUNNER_TEMP/shard-plugin/"' in suite["run"]
    assert 'uv run python "$RUNNER_TEMP/shard-plugin/python_shards.py" pytest ' in suite["run"] and "uv run pytest" not in suite["run"]
    upload = named["Retain shard evidence even when its tests fail"]
    assert upload["if"] == "${{ always() }}" and upload["with"]["name"] == "ci-evidence-python-${{ matrix.shard }}"
    assert upload["with"]["overwrite"] is True and upload["with"]["if-no-files-found"] == "error"
    names = [step.get("name") for step in python["steps"]]
    assert names.index("Python/PostgreSQL tests") < names.index("Record the shard outcome") < names.index(
        "Retain shard evidence even when its tests fail") == len(names) - 1
    # No receiving-commit check of its own: evidence requires its captured SHA to be verify's, which verify checks,
    # so a shard job's result is its tests' result, never a baseline's deliberate mismatch.
    assert not any("github.sha" in json.dumps(step) and step.get("with", {}).get("path") != "ci-tools" for step in python["steps"])


@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled", "skipped"])
def test_the_shard_outcome_step_records_only_its_python_check(tmp_path, outcome):
    step = next(step for step in jobs()["python-2"]["steps"] if step.get("name") == "Record the shard outcome")
    assert step["if"] == "${{ always() }}" and step["env"] == {"PYTHON_OUTCOME": "${{ steps.python.outcome }}"}
    (tmp_path / "ci-evidence").mkdir()
    subprocess.run(["bash", "-e", "-c", step["run"]], check=True, env={"PATH": os.environ["PATH"], "RUNNER_TEMP": str(tmp_path),
                                                                    "PYTHON_OUTCOME": outcome})
    assert json.loads((tmp_path / "ci-evidence" / "outcomes.json").read_text()) == {"checks": {"python": outcome}}


def shard_fragment(fragments, job, **files):
    fragment = fragments / f"ci-evidence-{job}"
    for name, content in files.items():
        if content is None:
            (fragment / name.replace("_", ".", 1)).unlink()
        else:
            (fragment / name.replace("_", ".", 1)).write_text(content)


@pytest.mark.parametrize("change, producer, python", [
    ({}, None, "passed"),
    ({"outcomes_json": json.dumps({"checks": {"python": "failure"}})}, ("failure", "a" * 40), "failed"),
    ({"outcomes_json": json.dumps({"checks": {"python": "cancelled"}})}, ("cancelled", "a" * 40), "blocked"),
    ({"python-3_xml": None}, None, "blocked"),
    ({"python-3_xml": '<testsuite><testcase name="t" file="tests/t.py"><failure/></testcase></testsuite>'}, None, "failed"),
    ({"python-3_xml": "<!DOCTYPE x><testsuite/>"}, None, "blocked"),
    ({"python-3_xml": "<testsuite"}, None, "blocked"),
    ({"attempt": "1\n"}, ("failure", "a" * 40), "failed"),
    # Each shard's report is judged on its own, so the other shards' passing cases never hide it.
    ({"python-3_xml": '<testsuite><testcase name="t" file="tests/t.py"><skipped/></testcase></testsuite>'}, None, "blocked"),
    ({"python-3_xml": "<testsuite/>"}, None, "blocked"),
    ({"python-3_xml": '<testsuites><testcase name="lost" file="tests/lost.py"><failure/></testcase></testsuites>'}, None, "blocked"),
    # A shard job that failed or was cancelled after uploading a passing fragment of this attempt never passes.
    ({}, ("failure", "a" * 40), "failed"),
    ({}, ("cancelled", "a" * 40), "blocked"),
    ({}, ("skipped", "a" * 40), "blocked"),
], ids=["all-passed", "failed-shard", "cancelled-shard", "no-report", "failing-case", "unsafe-report", "malformed-report",
        "stale-fragment-of-a-failed-rerun", "only-skipped-cases", "empty-report", "case-outside-a-suite",
        "job-failed-after-upload", "job-cancelled-after-upload", "job-skipped"])
def test_the_python_check_passes_only_when_every_shard_passed_with_its_report(tmp_path, change, producer, python):
    fragments = evidence_fragments(tmp_path)
    shard_fragment(fragments, "python-3", **change)
    merged = tiden_ci.assemble(tmp_path / "ci-evidence", fragments, ATTEMPT_URL, {**PASSED, **({"python-3": producer} if producer else {})})
    assert assembled_rows(tmp_path, merged)["anymsg-ci::python"] == python
    assert ("python-3" in merged["missing"]) == ("attempt" in change)
    # Shard reports never reach the evidence under their own names; only the merged python report does.
    assert not {path.name for path in (tmp_path / "ci-evidence").iterdir()} & set(tiden_ci.SHARD_REPORTS)


def test_a_missing_shard_or_a_failed_verify_shard_never_reads_as_passed(tmp_path):
    missing = tiden_ci.assemble(tmp_path / "a" / "ci-evidence", evidence_fragments(tmp_path / "a", shards=tuple(job for job in tiden_ci.SHARD_JOBS if job != "python-3")),
                                ATTEMPT_URL, PASSED)
    assert missing["missing"] == ["python-3"] and missing["checks"]["python"] == "skipped"
    assert assembled_rows(tmp_path / "a", missing)["anymsg-ci::python"] == "blocked"
    fragments = evidence_fragments(tmp_path / "b")
    metadata = json.loads((fragments / "ci-evidence-verify" / "metadata.json").read_text())
    (fragments / "ci-evidence-verify" / "metadata.json").write_text(json.dumps({**metadata, "checks": {**metadata["checks"], "python": "failure"}}))
    failed = tiden_ci.assemble(tmp_path / "b" / "ci-evidence", fragments, ATTEMPT_URL, PASSED)
    assert failed["checks"]["python"] == "failure" and failed["checks"]["smoke"] == "success"


def test_a_shard_fragment_may_report_only_its_python_check_of_verifys_commit(tmp_path):
    for index, outcomes in enumerate([{"checks": {"python": "success", "smoke": "success"}}, {"module_checks": {}},
                                      {"checks": {"python": "success"}, "module_checks": {"load": "success"}}]):
        fragments = evidence_fragments(tmp_path / str(index))
        shard_fragment(fragments, "python-2", outcomes_json=json.dumps(outcomes))
        with pytest.raises(ValueError, match="only its own"):
            tiden_ci.assemble(tmp_path / str(index) / "ci-evidence", fragments, ATTEMPT_URL, PASSED)
    fragments = evidence_fragments(tmp_path / "sha")
    shard_fragment(fragments, "python-4", **{"source-sha": "b" * 40 + "\n"})
    with pytest.raises(ValueError, match="another source"):
        tiden_ci.assemble(tmp_path / "sha" / "ci-evidence", fragments, ATTEMPT_URL, PASSED)
    with pytest.raises(ValueError, match="captured another source"):
        tiden_ci.assemble(tmp_path / "captured" / "ci-evidence", evidence_fragments(tmp_path / "captured"), ATTEMPT_URL,
                          {**PASSED, "python-2": ("success", "b" * 40)})
    fragments = evidence_fragments(tmp_path / "foreign")
    shard_fragment(fragments, "python-2", **{"load_xml": shard_report("x")})
    with pytest.raises(ValueError, match="does not produce"):
        tiden_ci.assemble(tmp_path / "foreign" / "ci-evidence", fragments, ATTEMPT_URL, PASSED)


def test_the_python_producer_names_every_shard_job_of_the_matrix(tmp_path):
    evidence_fragments(tmp_path)
    env = {"PATH": os.environ["PATH"], "GITHUB_REPOSITORY": "example/repo", "GITHUB_RUN_ID": "1", "GITHUB_RUN_ATTEMPT": "2"}
    base = [sys.executable, str(ROOT / "scripts/tiden_ci.py"), "assemble", "--fragments", str(tmp_path / "fragments"),
            "--producer", "verify:success:" + "a" * 40, "--producer", "load:success:" + "a" * 40,
            "--producer", "go:success:" + "a" * 40, "--producer", "added:success:" + "a" * 40]
    shards = [argument for job in tiden_ci.SHARD_JOBS for argument in ("--producer", f"{job}:success:" + "a" * 40)]
    passed = subprocess.run([*base, "--directory", str(tmp_path / "ok"), *shards], env=env, capture_output=True, text=True)
    assert passed.returncode == 0, passed.stderr
    assert json.loads((tmp_path / "ok" / "metadata.json").read_text())["producers"].keys() == tiden_ci.FRAGMENTS.keys()
    # Without every shard job's own producer the assembly refuses, rather than counting the shards as passed.
    assert subprocess.run([*base, "--directory", str(tmp_path / "none")], env=env, capture_output=True).returncode != 0
    assert subprocess.run([*base, "--directory", str(tmp_path / "some"), *shards[:4]], env=env, capture_output=True).returncode != 0


def test_each_shard_job_reports_its_own_captured_source_to_the_assembly(tmp_path):
    # Each shard is a job of its own, so evidence receives its own completed result and captured SHA.
    all_jobs = jobs()
    merge = next(step for step in all_jobs["evidence"]["steps"]
                 if step.get("name") == "Merge the fragments of the tested commit and record every check outcome")
    for job in tiden_ci.SHARD_JOBS:
        index = job.split("-")[1]
        assert merge["env"][f"PYTHON_{index}"] == f"{job}:${{{{ needs.{job}.result }}}}:${{{{ needs.{job}.outputs.source_sha }}}}"
        assert f'--producer "$PYTHON_{index}"' in merge["run"]
    # One shard job's checkout differing from verify's is refused, whatever the other shard jobs captured.
    with pytest.raises(ValueError, match="captured another source"):
        tiden_ci.assemble(tmp_path / "other" / "ci-evidence", evidence_fragments(tmp_path / "other"), ATTEMPT_URL,
                          {**PASSED, "python-3": ("success", "b" * 40)})


@pytest.mark.parametrize("status", ["success", "failure", "cancelled"])
def test_a_baseline_of_another_source_keeps_its_shard_step_outcomes(tmp_path, status):
    # A shard job has no receiving-commit check, which a baseline of another source fails by design, so a
    # baseline's shard job result is its tests' result: passing shards count, and a failed or cancelled job never passes.
    for job in tiden_ci.SHARD_JOBS:
        names = [step.get("name") for step in jobs()[job]["steps"]]
        assert "Require the tested source to be the commit receiving this check" not in names
    merged = tiden_ci.assemble(tmp_path / "ci-evidence", evidence_fragments(tmp_path), ATTEMPT_URL,
                               {**PASSED, "python-3": (status, "a" * 40)})
    assert merged["checks"]["python"] == status and merged["producers"]["python-3"]["result"] == status


# The evidence step's needs context, as GitHub resolves it for these jobs; any other expression is unsupported here.
def resolve(template, needs):
    import re
    resolved = re.sub(r"\$\{\{ needs\.([a-z0-9-]+)\.(result|outputs\.source_sha) \}\}",
                      lambda match: needs[match.group(1)]["result" if match.group(2) == "result" else "sha"], template)
    assert "${{" not in resolved, resolved
    return resolved


@pytest.mark.parametrize("late, row", [("success", "passed"), ("failure", "failed"), ("cancelled", "blocked")])
def test_a_shard_job_that_fails_after_uploading_a_passing_fragment_never_reads_as_passed(tmp_path, late, row):
    # The job passed its tests and uploaded a passing fragment, then failed or was cancelled in a later step or a
    # post step. Evidence takes the job's completed result from `needs`, never a status sampled inside the job.
    root = Path(__file__).resolve().parents[1]
    merge = next(step for step in jobs()["evidence"]["steps"]
                 if step.get("name") == "Merge the fragments of the tested commit and record every check outcome")
    needs = {"verify": {"result": "success", "sha": "a" * 40}, "load": {"result": "success", "sha": "a" * 40},
             "go": {"result": "success", "sha": "a" * 40}, "added": {"result": "success", "sha": "a" * 40},
             **{job: {"result": "success", "sha": "a" * 40} for job in tiden_ci.SHARD_JOBS}}
    needs["python-3"]["result"] = late
    work = tmp_path / "work"
    work.mkdir()
    (work / "ci-tools").symlink_to(root, target_is_directory=True)
    temp = tmp_path / "runner"
    temp.mkdir()
    # Every fragment, python-3's included, reports passing tests of this attempt; prepare reads the real Go modules.
    evidence_fragments(temp, go_streams={check: go_stream(check) for check in tiden_ci.GO_MODULES})
    env = {"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin", "RUNNER_TEMP": str(temp), "GITHUB_REPOSITORY": "example/repo",
           "GITHUB_RUN_ID": "1", "GITHUB_RUN_ATTEMPT": "2", **{name: resolve(value, needs) for name, value in merge["env"].items()}}
    subprocess.run(["bash", "-e", "-c", merge["run"]], cwd=work, capture_output=True, text=True, env=env)
    rows = json.loads((temp / "ci-evidence" / "ci-checks.json").read_text())
    assert next(r for r in rows if r["signature"] == "anymsg-ci::python")["execution"]["status"] == row
    producers = json.loads((temp / "ci-evidence" / "metadata.json").read_text())["producers"]
    assert producers["python-3"]["result"] == late


def test_a_partial_rerun_keeps_the_untouched_shard_jobs_fragments(tmp_path):
    # Attempt 1 passed shards 2 and 4 and failed shard 3; the rerun of shard 3 failed again. With each shard job's
    # own result, the untouched passing shards keep their attempt-1 fragments and reports.
    fragments = evidence_fragments(tmp_path)
    for job in ("python-2", "python-4"):
        shard_fragment(fragments, job, attempt="1\n")
    shard_fragment(fragments, "python-3", outcomes_json=json.dumps({"checks": {"python": "failure"}}))
    producers = {**PASSED, "python-3": ("failure", "a" * 40)}
    merged = tiden_ci.assemble(tmp_path / "ci-evidence", fragments, ATTEMPT_URL, producers)
    assert merged["missing"] == [] and merged["checks"]["python"] == "failure"
    assert {job: merged["producers"][job]["attempt"] for job in tiden_ci.SHARD_JOBS} == {
        job: "1" if job in ("python-2", "python-4") else "2" for job in tiden_ci.SHARD_JOBS}
    names = [case.get("name") for case in ET.parse(tmp_path / "ci-evidence" / "python.xml").getroot().iter("testcase")]
    assert names == ["x", *tiden_ci.SHARD_JOBS]
