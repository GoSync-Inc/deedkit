"""The root Python suite runs in four file-level shards on parallel runners; synthetic workflows and reports only."""
import json
import os
from pathlib import Path
import random
import subprocess
import sys

import pytest
import yaml

from scripts import python_shards, tiden_ci
from tests.test_tiden_ci import ATTEMPT_URL, PASSED, assembled_rows, evidence_fragments, shard_report

ROOT = Path(__file__).resolve().parents[1]
WEIGHTS = json.loads(python_shards.WEIGHTS.read_text())


def jobs():
    return yaml.safe_load((ROOT / ".github/workflows/checks.yml").read_text())["jobs"]


def test_every_test_module_runs_in_exactly_one_of_the_four_shards():
    modules = python_shards.modules(ROOT)
    assert "tests/test_python_shards.py" in modules and "tests/platform/test_stack.py" in modules
    assert all(name.startswith("tests/") and Path(name).name.startswith("test_") for name in modules)
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
    real = python_shards.plan(python_shards.modules(ROOT), 4, WEIGHTS)
    seconds = [sum(WEIGHTS["seconds"].get(name, WEIGHTS["default"]) for name, owner in real.items() if owner == index)
               for index in range(1, 5)]
    seconds[0] += WEIGHTS["head_start"]
    assert max(seconds) - min(seconds) < 60, seconds


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


def collected(directory, shard=None):
    env = {key: value for key, value in os.environ.items() if key != "DEEDKIT_TEST_SHARD"}
    if shard:
        env["DEEDKIT_TEST_SHARD"] = shard
    result = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", directory],
                            cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
    return result.returncode, sorted(line for line in result.stdout.splitlines() if line.startswith("tests/"))


def test_pytest_collects_each_module_in_exactly_the_shard_the_plan_names():
    # tests/conftest.py applies the shard while pytest collects; the four shards together are the full collection.
    code, full = collected("tests/platform")
    assert code == 0 and len(full) >= 5
    shards = [collected("tests/platform", f"{index}/4") for index in range(1, 5)]
    union = [line for code, lines in shards for line in lines]
    assert sorted(union) == full and len(set(union)) == len(union)
    plan = python_shards.plan(python_shards.modules(ROOT), 4, WEIGHTS)
    for index, (code, lines) in enumerate(shards, 1):
        assert code in (0, 5)  # 5: this shard owns no module of the directory
        assert all(plan[line.split(":")[0]] == index for line in lines)
    code, _ = collected("tests/platform", "5/4")
    assert code == 4  # a usage error, so CI never silently runs another shard's modules


def test_python_shard_jobs_run_verifys_suite_step_on_runners_of_their_own():
    all_jobs = jobs()
    verify, python = all_jobs["verify"], all_jobs["python"]
    assert python["strategy"] == {"fail-fast": False, "matrix": {"shard": list(range(2, tiden_ci.PYTHON_SHARDS + 1))}}
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
                 "Install the pinned gitleaks for the image-scan tests", "Wait for the background router image and Python image",
                 "Require the tested source to be the commit receiving this check"):
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
    upload = named["Retain shard evidence even when its tests fail"]
    assert upload["if"] == "${{ always() }}" and upload["with"]["name"] == "ci-evidence-python-${{ matrix.shard }}"
    assert upload["with"]["overwrite"] is True and upload["with"]["if-no-files-found"] == "error"
    names = [step.get("name") for step in python["steps"]]
    assert names.index("Python/PostgreSQL tests") < names.index("Record the shard outcome") < names.index(
        "Retain shard evidence even when its tests fail") < names.index("Require the tested source to be the commit receiving this check")


@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled", "skipped"])
def test_the_shard_outcome_step_records_only_its_python_check(tmp_path, outcome):
    step = next(step for step in jobs()["python"]["steps"] if step.get("name") == "Record the shard outcome")
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
], ids=["all-passed", "failed-shard", "cancelled-shard", "no-report", "failing-case", "unsafe-report", "malformed-report",
        "stale-fragment-of-a-failed-rerun"])
def test_the_python_check_passes_only_when_every_shard_passed_with_its_report(tmp_path, change, producer, python):
    fragments = evidence_fragments(tmp_path)
    shard_fragment(fragments, "python-3", **change)
    merged = tiden_ci.assemble(tmp_path / "ci-evidence", fragments, ATTEMPT_URL, {**PASSED, **({"python-3": producer} if producer else {})})
    assert assembled_rows(tmp_path, merged)["anymsg-ci::python"] == python
    assert ("python-3" in merged["missing"]) == ("attempt" in change)
    # Shard reports never reach the evidence under their own names; only the merged python report does.
    assert not {path.name for path in (tmp_path / "ci-evidence").iterdir()} & set(tiden_ci.SHARD_REPORTS)


def test_a_missing_shard_or_a_failed_verify_shard_never_reads_as_passed(tmp_path):
    missing = tiden_ci.assemble(tmp_path / "a" / "ci-evidence", evidence_fragments(tmp_path / "a", shards=("python-2", "python-4")),
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
            "--producer", "verify:success:" + "a" * 40, "--producer", "load:success:" + "a" * 40]
    passed = subprocess.run([*base, "--directory", str(tmp_path / "ok"), "--producer", "python:success:" + "a" * 40],
                            env=env, capture_output=True, text=True)
    assert passed.returncode == 0, passed.stderr
    assert json.loads((tmp_path / "ok" / "metadata.json").read_text())["producers"].keys() == tiden_ci.FRAGMENTS.keys()
    # Without the matrix job's result the assembly refuses, rather than counting the shards as passed.
    assert subprocess.run([*base, "--directory", str(tmp_path / "none")], env=env, capture_output=True).returncode != 0
