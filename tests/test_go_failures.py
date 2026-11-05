"""The CI log's list of failed Go tests, from synthetic `go test -json` streams; it never runs Go."""
import json
from pathlib import Path
import subprocess
import sys

from hypothesis import given
from hypothesis import strategies as st
import yaml

import scripts.go_failures as go_failures

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/go_failures.py"
PACKAGE = "example.test/adapter"


def event(action, package=PACKAGE, test=None, **fields):
    return json.dumps({"Action": action, "Package": package, **({"Test": test} if test else {}), **fields})


def output(line, test=None, package=PACKAGE):
    return event("output", package, test, Output=line + "\n")


def failed_test(test, count, package=PACKAGE):
    return [event("run", package, test), *(output(f"{test} line {index}", test, package) for index in range(count)),
            event("fail", package, test)]


def run(tmp_path, lines, *, raw=b""):
    path = tmp_path / "stream.json"
    path.write_bytes("".join(line + "\n" for line in lines).encode() + raw)
    return subprocess.run([sys.executable, str(SCRIPT), str(path)], capture_output=True, text=True, check=False)


# spec: ANYM-801
def test_a_failed_test_is_named_with_its_output_bounded_at_forty_lines(tmp_path):
    done = run(tmp_path, [*failed_test("TestBoom", 45), event("fail")])
    lines = done.stdout.splitlines()
    assert done.returncode == 0 and lines[0] == f"FAIL {PACKAGE} TestBoom"
    assert lines[1:] == [f"TestBoom line {index}" for index in range(40)]


# spec: ANYM-801
def test_two_packages_print_only_the_failed_one_and_its_failed_tests(tmp_path):
    other = "example.test/other"
    done = run(tmp_path, [
        event("run", other, "TestFine"), output("fine noise", "TestFine", other), event("pass", other, "TestFine"), event("pass", other),
        event("run", PACKAGE, "TestPasses"), event("pass", PACKAGE, "TestPasses"), *failed_test("TestBoom", 1),
        output("package noise"), event("fail")])
    assert done.stdout.splitlines() == [f"FAIL {PACKAGE} TestBoom", "TestBoom line 0"]


# spec: ANYM-801
def test_a_build_failure_names_the_package_with_the_compiler_output(tmp_path):
    build = f"{PACKAGE} [{PACKAGE}.test]"
    done = run(tmp_path, [
        json.dumps({"ImportPath": build, "Action": "build-output", "Output": "./x_test.go:3:1: syntax error\n"}),
        json.dumps({"ImportPath": build, "Action": "build-fail"}),
        event("start"), output(f"FAIL\t{PACKAGE} [build failed]"), event("fail", FailedBuild=build)])
    assert done.stdout.splitlines() == [f"FAIL {PACKAGE}", "./x_test.go:3:1: syntax error", f"FAIL\t{PACKAGE} [build failed]"]


# spec: ANYM-801
def test_a_build_failure_no_package_event_claims_is_still_named(tmp_path):
    done = run(tmp_path, [
        json.dumps({"ImportPath": "example.test/broken", "Action": "build-output", "Output": "broken.go:1:1: bad\n"}),
        json.dumps({"ImportPath": "example.test/broken", "Action": "build-fail"})])
    assert done.stdout.splitlines() == ["FAIL example.test/broken", "broken.go:1:1: bad"]


# spec: ANYM-801
def test_a_package_failure_after_its_tests_prints_the_package_output(tmp_path):
    done = run(tmp_path, [
        event("run", test="TestFine"), event("pass", test="TestFine"),
        output("WARNING: DATA RACE"), output("FAIL"), event("fail")])
    assert done.stdout.splitlines() == [f"FAIL {PACKAGE}", "WARNING: DATA RACE", "FAIL"]


# spec: ANYM-801
def test_a_test_left_running_by_a_failed_package_is_named(tmp_path):
    done = run(tmp_path, [
        event("run", test="TestHangs"), output("panic: test timed out", "TestHangs"), output("FAIL"), event("fail")])
    assert done.stdout.splitlines() == [
        f"FAIL {PACKAGE}", "FAIL", f"FAIL {PACKAGE} TestHangs", "panic: test timed out"]


# spec: ANYM-801
def test_a_timeout_after_an_earlier_failed_test_still_names_the_test_left_running(tmp_path):
    done = run(tmp_path, [
        *failed_test("TestBoom", 1), event("run", test="TestHangs"), output("panic: test timed out", "TestHangs"),
        output("FAIL"), event("fail")])
    assert done.stdout.splitlines() == [
        f"FAIL {PACKAGE} TestBoom", "TestBoom line 0", f"FAIL {PACKAGE} TestHangs", "panic: test timed out"]


# spec: ANYM-801
def test_a_truncated_last_line_and_other_unreadable_lines_are_counted_not_fatal(tmp_path):
    done = run(tmp_path, ["not json", "[1]", json.dumps({"Action": 1}), *failed_test("TestBoom", 1)],
               raw=b'{"Action":"out\xd0')
    assert done.returncode == 0
    assert done.stdout.splitlines() == [f"FAIL {PACKAGE} TestBoom", "TestBoom line 0"]
    assert "4 unreadable lines" in done.stderr


# spec: ANYM-801
def test_an_empty_file_and_a_passing_file_print_nothing(tmp_path):
    for lines in ([], [event("run", test="TestFine"), output("noise", "TestFine"), event("pass", test="TestFine"), event("pass")]):
        done = run(tmp_path, lines)
        assert (done.returncode, done.stdout, done.stderr) == (0, "", "")


# spec: ANYM-801
def test_the_output_stops_at_two_hundred_lines_with_a_final_count(tmp_path):
    lines = [line for index in range(10) for line in failed_test(f"TestBoom{index}", 40)]
    printed = run(tmp_path, lines).stdout.splitlines()
    assert len(printed) == 200 and printed[-1] == f"... {410 - 199} more"
    assert printed[:-1] == go_failures.report(line.encode() for line in lines)[0][:199]


# spec: ANYM-801
def test_a_missing_file_says_so_and_exits_zero(tmp_path):
    done = subprocess.run([sys.executable, str(SCRIPT), str(tmp_path / "absent.json")], capture_output=True, text=True, check=False)
    assert (done.returncode, done.stdout) == (0, "no go test output file\n")


# spec: ANYM-801
def test_output_a_terminal_cannot_encode_does_not_crash_the_script(tmp_path):
    path = tmp_path / "stream.json"
    path.write_text(event("fail", test="TestBoom") + "\n" + event("output", test="TestBoom", Output="\ud800 odd\n") + "\n")
    done = subprocess.run([sys.executable, str(SCRIPT), str(path)], capture_output=True, check=False)
    assert done.returncode == 0 and b"odd" in done.stdout


# spec: ANYM-801
def test_environment_values_are_not_printed(tmp_path):
    path = tmp_path / "stream.json"
    path.write_text("\n".join(failed_test("TestBoom", 1)) + "\n")
    done = subprocess.run([sys.executable, str(SCRIPT), str(path)], capture_output=True, text=True, check=False,
                          env={"GO_FAILURES_CANARY": "canary-value"})
    assert "canary-value" not in done.stdout + done.stderr


# spec: ANYM-801
@given(st.lists(st.integers(0, 60), max_size=20))
def test_every_failed_test_line_is_printed_or_counted(counts):
    """Conservation: the printed lines plus the final count are exactly what the per-test bound leaves."""
    stream = [line for index, count in enumerate(counts) for line in failed_test(f"Test{index}", count)]
    lines, skipped = go_failures.report(line.encode() for line in stream)
    printed = go_failures.bounded(lines, skipped)
    total = sum(1 + min(count, 40) for count in counts)
    assert skipped == 0 and len(lines) == total
    if total <= 200:
        assert printed == lines
    else:
        assert len(printed) == 200 and printed[:199] == lines[:199] and printed[-1] == f"... {total - 199} more"


# spec: ANYM-801
@given(st.lists(st.one_of(st.binary(max_size=40), st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(), st.sampled_from(["fail", "output", "build-fail", "x"])),
    lambda inner: st.one_of(st.lists(inner, max_size=3), st.dictionaries(
        st.sampled_from(["Action", "Package", "Test", "ImportPath", "Output", "FailedBuild"]), inner, max_size=6)),
    max_leaves=8).map(lambda value: json.dumps(value).encode())), max_size=30))
def test_any_stream_is_read_without_an_error_and_stays_in_the_cap(stream):
    lines, skipped = go_failures.report(iter(stream))
    assert skipped <= len(stream) and len(go_failures.bounded(lines, skipped)) <= 200


# spec: ANYM-801
def test_the_workflow_runs_the_script_only_after_the_go_step_failed():
    steps = yaml.safe_load((ROOT / ".github/workflows/checks.yml").read_text())["jobs"]["go"]["steps"]
    index = next(position for position, step in enumerate(steps) if step.get("id") == "whatsmeow")
    assert "go test -race -count=1 -json ./..." in steps[index]["run"] and "go_failures" not in steps[index]["run"]
    following = steps[index + 1]
    assert following["if"] == "${{ !cancelled() && steps.whatsmeow.outcome == 'failure' }}"
    assert following["run"] == 'python3 ci-tools/scripts/go_failures.py "$RUNNER_TEMP/ci-evidence/whatsmeow.json"'
    assert "id" not in following and "continue-on-error" not in following
