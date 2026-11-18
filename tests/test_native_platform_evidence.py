"""DK65: a hosted Windows runner's Node JUnit maps to the canonical repository identity, and nothing else does.

The Linux row is the real native Node reporter's output for the one Windows-only case; the Windows row is that
same report with the absolute `file` a hosted Windows checkout writes. Nothing here calls Tiden.
"""
from pathlib import Path
import shutil
import subprocess
import xml.etree.ElementTree as ET

import pytest

from scripts.tiden_ci import normalize_junit, windows_source_path

ROOT = Path(__file__).resolve().parents[1]
CASE = "a Windows alternate-stream output name is refused"
FILE = "packages/deedkit/test/download.test.js"
ALLOWED = frozenset({FILE})
WORKSPACE = "D:\\a\\deedkit\\deedkit"


def linux_report():
    """The native reporter's JUnit for exactly the Windows-only case, run on this (non-Windows) host."""
    node = shutil.which("node")
    if not node:
        pytest.fail("node is required: the DeedKit package tests run under node --test")
    run = subprocess.run([node, "--test", "--test-reporter=junit", f"--test-name-pattern=^{CASE}$", "test/download.test.js"],
                         cwd=ROOT / "packages/deedkit", capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stdout[-2000:]
    return run.stdout


def identity(path, **windows):
    xml, counts = normalize_junit(path, ROOT, "node", "packages/deedkit", **windows)
    case, = ET.fromstring(xml).iter("testcase")
    return (case.get("classname"), case.get("name"), case.get("file")), counts


# spec: ANYM-261
def test_the_filtered_native_run_emits_only_the_selected_case():
    cases = list(ET.fromstring(linux_report()).iter("testcase"))
    assert [case.get("name") for case in cases] == [CASE]
    assert cases[0].find("skipped") is not None  # off Windows the case keeps its own skip


# spec: ANYM-261
def test_a_windows_row_normalizes_to_the_same_identity_as_the_linux_row(tmp_path):
    linux = tmp_path / "linux.xml"
    linux.write_text(linux_report())
    document = ET.fromstring(linux.read_text())
    case, = document.iter("testcase")
    case.set("file", WORKSPACE + "\\packages\\deedkit\\test\\download.test.js")
    case.remove(case.find("skipped"))  # as the Windows runner executes it
    windows = tmp_path / "windows.xml"
    windows.write_bytes(ET.tostring(document))
    base, base_counts = identity(linux)
    native, native_counts = identity(windows, windows_workspace=WORKSPACE, allowed_files=ALLOWED)
    assert native == base == ("node:" + FILE, CASE, FILE)
    assert (base_counts["skipped"], native_counts["passed"]) == (1, 1)


# spec: ANYM-261
def test_the_windows_workspace_path_maps_only_the_allowed_file():
    assert windows_source_path(WORKSPACE + "\\packages\\deedkit\\test\\download.test.js", WORKSPACE, ALLOWED) == FILE


# spec: ANYM-261
@pytest.mark.parametrize("value", [
    "D:\\a\\deedkit\\deedkit\\packages\\deedkit\\test\\client.test.js",       # another file of the package
    "C:\\a\\deedkit\\deedkit\\packages\\deedkit\\test\\download.test.js",     # another drive
    "D:\\a\\deedkit\\deedkit-x\\packages\\deedkit\\test\\download.test.js",   # a sibling sharing the prefix text
    "D:\\a\\deedkit\\deedkit\\packages\\..\\deedkit\\test\\download.test.js",  # traversal
    "D:\\a\\deedkit\\deedkit\\packages\\deedkit\\test\\\\download.test.js",   # an empty part
    "D:\\a\\deedkit\\deedkit\\packages/deedkit/test/download.test.js",        # mixed separators
    "\\\\server\\share\\packages\\deedkit\\test\\download.test.js",           # UNC
    "\\\\?\\D:\\a\\deedkit\\deedkit\\packages\\deedkit\\test\\download.test.js",  # device path
    "packages/deedkit/test/download.test.js",                                  # not under the workspace
    "", None,
])
def test_a_windows_file_outside_the_rule_refuses(value):
    with pytest.raises(ValueError):
        windows_source_path(value, WORKSPACE, ALLOWED)


# spec: ANYM-261
@pytest.mark.parametrize("workspace", ["", "D:", "D:\\", "D:\\a\\..\\b", "D:\\a\\.", "a\\b", "\\\\server\\share",
                                       "D:/a/deedkit", "D:\\a\\deedkit\\", "D:\\a\\dee dkit", None, 3])
def test_a_malformed_declared_workspace_refuses(workspace):
    with pytest.raises(ValueError):
        windows_source_path("D:\\a\\deedkit\\deedkit\\packages\\deedkit\\test\\download.test.js", workspace, ALLOWED)


# spec: ANYM-261
def test_a_windows_report_with_one_refused_row_is_refused_whole(tmp_path):
    report = tmp_path / "windows.xml"
    report.write_text("<testsuite>"
                      f'<testcase name="{CASE}" classname="test" file="{WORKSPACE}\\packages\\deedkit\\test\\download.test.js"/>'
                      f'<testcase name="x" classname="test" file="{WORKSPACE}\\packages\\deedkit\\test\\cli.test.js"/>'
                      "</testsuite>")
    with pytest.raises(ValueError, match="not one this module may report"):
        normalize_junit(report, ROOT, "node", "packages/deedkit", windows_workspace=WORKSPACE, allowed_files=ALLOWED)


def publisher(monkeypatch, tmp_path, *, platform=(), fail=None):
    """publish() against a recording fake CLI; `fail` names the report file whose upload call fails."""
    import json
    import subprocess as sp
    from scripts.tiden_ci import CHECKS
    calls = []

    def run(args, **kwargs):
        calls.append(args[1:])
        command = args[1:3]
        value = ({"branches": [{"id": "branch-id", "name": "intent/feature"}]} if command == ["branch", "list"] else
                 {"seqNum": 3, "branchName": "intent/feature", "buildSha": "a" * 40} if command == ["run", "create"] else
                 {"status": "passed", "branchId": "branch-id", "liveDocStatus": "succeeded",
                  "stats": {"attempts": str(len(CHECKS) + len(platform))}} if command == ["run", "get"] else
                 {"status": "passed"} if command == ["run", "complete"] else {})
        failed = command == ["run", "report"] and fail is not None and any(a.endswith("/" + fail) for a in args)
        return sp.CompletedProcess(args, 1 if failed else 0, stdout=json.dumps(value), stderr="")
    monkeypatch.setattr(sp, "run", run)
    (tmp_path / "ci-checks.json").write_text("[]")
    for name in platform:
        (tmp_path / name).write_text("<testsuite/>")
    manifest = {"sha": "a" * 40, "git_branch": "codex/feature", "ci_url": "https://github.com/example/repo/actions/runs/1",
                "checks": dict.fromkeys(CHECKS, "success"), "schema": 1, "reports": ["ci-checks.json"],
                "test_counts": {"passed": len(platform)} if platform else {}, "check_counts": {"passed": len(CHECKS)},
                **({"platform_reports": list(platform)} if platform else {})}
    return calls, manifest


def sequence(calls):
    return [(c[0:2], [Path(a).name for a in c[3:] if a.startswith("/")] if c[0:2] == ["run", "report"] else [])
            for c in calls if c[0] == "run"]


# spec: ANYM-261
def test_platform_reports_go_in_one_second_call_after_the_base_call_and_before_complete(monkeypatch, tmp_path):
    from scripts.tiden_ci import publish
    calls, manifest = publisher(monkeypatch, tmp_path, platform=("deedkit-npm_windows.xml", "android_vision.xml"))
    assert publish(tmp_path, manifest, "intent/feature", "product")["status"] == "passed"
    runs = [step for step in sequence(calls) if step[0] != ["run", "get"]]
    assert runs == [(["run", "create"], []), (["run", "report"], ["ci-checks.json"]),
                    (["run", "report"], ["deedkit-npm_windows.xml", "android_vision.xml"]), (["run", "complete"], [])]


# spec: ANYM-261
def test_without_platform_reports_the_run_keeps_one_report_call(monkeypatch, tmp_path):
    from scripts.tiden_ci import publish
    calls, manifest = publisher(monkeypatch, tmp_path)
    publish(tmp_path, manifest, "intent/feature", "product")
    assert [step for step in sequence(calls) if step[0] == ["run", "report"]] == [(["run", "report"], ["ci-checks.json"])]


# spec: ANYM-261
def test_a_failed_platform_call_aborts_the_run_once_with_no_retry_and_no_complete(monkeypatch, tmp_path):
    from scripts.tiden_ci import publish
    calls, manifest = publisher(monkeypatch, tmp_path, platform=("deedkit-npm_windows.xml",), fail="deedkit-npm_windows.xml")
    with pytest.raises(RuntimeError, match="platform report failed"):
        publish(tmp_path, manifest, "intent/feature", "product")
    assert [step[0] for step in sequence(calls)] == [["run", "create"], ["run", "report"], ["run", "report"], ["run", "abort"]]


# spec: ANYM-261
@pytest.mark.parametrize("names", [["ci-checks.json"], ["x.xml", "x.xml"], ["../x.xml"], ["absent.xml"], "x.xml"])
def test_a_malformed_platform_list_refuses_before_any_report_upload(monkeypatch, tmp_path, names):
    from scripts.tiden_ci import publish
    calls, manifest = publisher(monkeypatch, tmp_path, platform=("x.xml",))
    manifest["platform_reports"] = names
    with pytest.raises(ValueError):
        publish(tmp_path, manifest, "intent/feature", "product")
    assert ["run", "report"] not in [step[0] for step in sequence(calls)]


VISION = "test_vision_corpus_character_error_rate_and_list_items_meet_the_bar"
VISION_PASS = (f'<testsuite><testcase classname="tests.test_screen_quality" name="{VISION}" '
               'file="tests/test_screen_quality.py" time="1.0"/></testsuite>')
WINDOWS_PASS = (f'<testsuite><testcase name="{CASE}" classname="test" '
                f'file="{WORKSPACE}\\packages\\deedkit\\test\\download.test.js" time="0.01"/></testsuite>')


def prepared(tmp_path, *, vision=VISION_PASS, windows=WINDOWS_PASS, workspace=WORKSPACE, outcomes=None):
    """The real `prepare` on the two native reports (other modules' reports absent): {signature: row}, manifest."""
    import json
    from scripts.tiden_ci import CHECKS, MODULE_REPORTS, prepare
    if vision is not None:
        (tmp_path / "android-vision.xml").write_text(vision)
    if windows is not None:
        (tmp_path / "deedkit-npm-windows.xml").write_text(windows)
    if workspace is not None:
        (tmp_path / "checkout-root").write_text(workspace + "\n")
    meta = {"sha": "a" * 40, "git_branch": "codex/feature", "ci_url": "https://github.com/example/repo/actions/runs/1",
            "checks": dict.fromkeys(CHECKS, "success"),
            "module_checks": {**dict.fromkeys(MODULE_REPORTS, "success"), **(outcomes or {})}}
    manifest = prepare(ROOT, tmp_path, meta)
    return {row["signature"]: row for row in json.loads((tmp_path / "ci-checks.json").read_text())}, manifest


def only_case(tmp_path, check):
    case, = ET.fromstring((tmp_path / f"normalized-{check}.xml").read_bytes()).iter("testcase")
    return case.get("classname"), case.get("name"), case.get("file")


# spec: ANYM-261
def test_native_reports_go_to_the_second_phase_under_the_canonical_identities(tmp_path):
    rows, manifest = prepared(tmp_path)
    assert manifest["platform_reports"] == ["normalized-android_vision.xml", "normalized-deedkit_npm_windows.xml"]
    assert not set(manifest["platform_reports"]) & set(manifest["reports"])
    assert rows["deedkit-ci::android_vision"]["execution"]["status"] == "passed"
    assert rows["deedkit-ci::deedkit_npm_windows"]["execution"]["status"] == "passed"
    # Test 17152's identity, and the identity of the Linux deedkit_npm row of the win32 case.
    assert only_case(tmp_path, "android_vision") == ("services.android.tests.test_screen_quality", VISION,
                                                     "services/android/tests/test_screen_quality.py")
    assert only_case(tmp_path, "deedkit_npm_windows") == ("node:" + FILE, CASE, FILE)


# spec: ANYM-261
@pytest.mark.parametrize("report", ["skipped", "failed", "extra", "twice", "other"])
def test_a_native_report_without_exactly_its_one_passed_case_fails_its_check(tmp_path, report):
    row = VISION_PASS[len("<testsuite>"):-len("</testsuite>")]
    vision = {"skipped": VISION_PASS.replace('time="1.0"/>', 'time="1.0"><skipped/></testcase>'),
              "failed": VISION_PASS.replace('time="1.0"/>', 'time="1.0"><failure/></testcase>'),
              "extra": VISION_PASS.replace("</testsuite>", row.replace(VISION, "test_other") + "</testsuite>"),
              "twice": VISION_PASS.replace("</testsuite>", row + "</testsuite>"),
              "other": VISION_PASS.replace(VISION, "test_other")}[report]
    rows, _ = prepared(tmp_path, vision=vision)
    assert rows["deedkit-ci::android_vision"]["execution"]["status"] in {"failed", "blocked"}


# spec: ANYM-261
@pytest.mark.parametrize("workspace", [None, "C:\\other", "\\\\server\\share"])
def test_a_windows_report_without_its_declared_checkout_root_is_no_verification(tmp_path, workspace):
    rows, manifest = prepared(tmp_path, workspace=workspace)
    assert rows["deedkit-ci::deedkit_npm_windows"]["execution"]["status"] == "blocked"
    assert "normalized-deedkit_npm_windows.xml" not in manifest.get("platform_reports", [])


# spec: ANYM-261
def test_a_passing_native_report_never_hides_a_failed_linux_module(tmp_path):
    (tmp_path / "deedkit-npm.xml").write_text(linux_report().replace('<skipped type="skipped" message="true"/>', "<failure/>"))
    rows, _ = prepared(tmp_path, outcomes={"deedkit_npm": "failure"})
    assert rows["deedkit-ci::deedkit_npm_windows"]["execution"]["status"] == "passed"
    assert rows["deedkit-ci::deedkit_npm"]["execution"]["status"] == "failed"


# spec: ANYM-261
def test_the_workflow_calls_both_native_jobs_and_the_evidence_job_requires_them():
    import yaml
    jobs = yaml.safe_load((ROOT / ".github/workflows/checks.yml").read_text())["jobs"]
    inputs = {name: "$" + "{{ inputs." + name + " }}" for name in ("source_ref", "added_tests")}
    for job, workflow in (("vision-macos", "vision-macos.yml"), ("deedkit-windows", "deedkit-windows.yml")):
        assert jobs[job]["uses"] == f"./.github/workflows/{workflow}" and jobs[job]["if"] == jobs["services"]["if"]
        assert jobs[job]["with"] == inputs and "permissions" not in jobs[job] and "secrets" not in jobs[job]
        assert job in jobs["evidence"]["needs"]
    steps = jobs["evidence"]["steps"]
    merge = next(step for step in steps if step.get("name", "").startswith("Merge the fragments"))
    assert '--producer "$VISION_MACOS" --producer "$DEEDKIT_WINDOWS"' in merge["run"]
    require = next(step for step in steps if step.get("name") == "Require every test job to have succeeded")
    assert 'test "$VISION_MACOS_RESULT" = success' in require["run"] and 'test "$DEEDKIT_WINDOWS_RESULT" = success' in require["run"]


def reused_evidence(tmp_path):
    """A passing queue run's real evidence (tests/test_ci_reuse.py's fixture), accepted as it stands."""
    from scripts import ci_reuse
    from tests.test_ci_reuse import PRODUCER_URL, SHA, queue_evidence
    evidence = queue_evidence(tmp_path)
    manifest = ci_reuse.validate(evidence, SHA, PRODUCER_URL, 2)
    assert manifest["platform_reports"] == ["normalized-android_vision.xml", "normalized-deedkit_npm_windows.xml"]
    return evidence, lambda: ci_reuse.validate(evidence, SHA, PRODUCER_URL, 2)


# spec: ANYM-261
@pytest.mark.parametrize("change", ["missing", "extra", "swapped", "in-base-list", "empty"])
def test_reused_evidence_without_exactly_the_ordered_native_report_list_is_refused(tmp_path, change):
    import json
    from scripts.ci_reuse import Refused
    evidence, validate = reused_evidence(tmp_path)
    manifest = json.loads((evidence / "manifest.json").read_text())
    native = manifest["platform_reports"]
    if change == "missing":
        del manifest["platform_reports"]
    elif change == "extra":
        manifest["platform_reports"] = [*native, "normalized-android.xml"]
    elif change == "swapped":
        manifest["platform_reports"] = native[::-1]
    elif change == "in-base-list":
        manifest["reports"] = [*manifest["reports"], native[0]]
        manifest["platform_reports"] = native[1:]
    else:
        manifest["platform_reports"] = []
    (evidence / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(Refused, match="does not list every required report"):
        validate()


# spec: ANYM-261
@pytest.mark.parametrize("report", ["skipped", "failed", "malformed"])
def test_reused_evidence_whose_native_report_did_not_pass_is_refused(tmp_path, report):
    from scripts.ci_reuse import Refused
    evidence, validate = reused_evidence(tmp_path)
    path = evidence / "normalized-deedkit_npm_windows.xml"
    if report == "malformed":
        path.write_text("<testsuite>")
    else:
        document = ET.fromstring(path.read_bytes())
        case, = document.iter("testcase")
        ET.SubElement(case, "skipped" if report == "skipped" else "failure")
        path.write_bytes(ET.tostring(document))
    with pytest.raises(Refused, match="deedkit_npm_windows report"):
        validate()
