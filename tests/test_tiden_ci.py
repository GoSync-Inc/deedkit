"""Reporting tests use synthetic JUnit and never call Tiden."""
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

import pytest

from scripts.tiden_ci import (CHECKS, FRAGMENTS, MODULE_REPORTS, PYTHON_SHARDS, REQUIRED_EXECUTIONS, SHARD_JOBS, intent_links, linked_intent_branch, normalize_junit, prepare, publish,
                              resolve_branch, route_branch)


def report(tmp_path, xml):
    path = tmp_path / "report.xml"
    path.write_text(xml)
    return path


def test_real_failure_and_skipped_outcomes_survive_normalization(tmp_path):
    path = report(tmp_path, '<testsuite><testcase name="a[1]" classname="tests.test_sample" file="tests/test_sample.py"/>'
                  '<testcase name="a[2]" classname="tests.test_sample" file="tests/test_sample.py"><failure/></testcase>'
                  '<testcase name="optional" classname="tests.test_sample" file="tests/test_sample.py"><skipped/></testcase></testsuite>')
    xml, counts = normalize_junit(path, tmp_path, "python")
    cases = list(ET.fromstring(xml).iter("testcase"))
    assert counts == Counter(passed=1, failed=1, skipped=1)
    assert [c.get("name") for c in cases] == ["a[1]", "a[2]", "optional"]
    assert cases[1].find("failure") is not None and cases[2].find("skipped") is not None


def test_node_titles_from_different_files_keep_distinct_identities(tmp_path):
    path = report(tmp_path, f'<testsuites><testcase name="equal title" classname="test" file="{tmp_path}/whatsapp/tests/a.test.mjs"/>'
                  f'<testcase name="equal title" classname="test" file="{tmp_path}/whatsapp/tests/b.test.mjs"/></testsuites>')
    xml, _ = normalize_junit(path, tmp_path, "collector")
    cases = list(ET.fromstring(xml).iter("testcase"))
    assert cases[0].get("classname") != cases[1].get("classname")
    assert cases[0].get("file") == "whatsapp/tests/a.test.mjs"


@pytest.mark.parametrize("xml", ['<!DOCTYPE testsuite [<!ENTITY x "secret">]><testsuite/>', '<testsuite/>',
                                 '<testsuite><testcase name="x" file="../outside.py"/></testsuite>',
                                 '<testsuite><testcase name="x" file="/outside.py"/></testsuite>',
                                 '<testsuite><testcase name="x"/></testsuite>'])
def test_untrusted_or_incomplete_junit_is_rejected(tmp_path, xml):
    with pytest.raises(ValueError):
        normalize_junit(report(tmp_path, xml), tmp_path, "python")


def metadata():
    return {"sha": "a" * 40, "git_branch": "codex/feature", "ci_url": "https://github.com/example/repo/actions/runs/1",
            "checks": {check: "success" for check in CHECKS}}


def test_missing_reports_and_unrun_checks_cannot_make_ci_green(tmp_path):
    meta = metadata()
    meta["checks"]["smoke"] = "skipped"
    summary = prepare(tmp_path, tmp_path, meta)
    assert summary["test_counts"] == {}
    assert summary["check_counts"] == {"passed": 3, "blocked": 4}
    checks = json.loads((tmp_path / "ci-checks.json").read_text())
    assert next(x for x in checks if x["signature"] == "anymsg-ci::smoke")["execution"]["status"] == "blocked"


def test_failed_test_overrides_misreported_successful_step(tmp_path):
    (tmp_path / "python.xml").write_text('<testsuite><testcase name="x" file="tests/x.py"><failure/></testcase></testsuite>')
    result = prepare(tmp_path, tmp_path, metadata())
    assert result["check_counts"]["failed"] == 1 and result["test_counts"]["failed"] == 1


def test_only_skipped_suite_does_not_count_as_success(tmp_path):
    (tmp_path / "python.xml").write_text('<testsuite><testcase name="x" file="tests/x.py"><skipped/></testcase></testsuite>')
    result = prepare(tmp_path, tmp_path, metadata())
    assert result["check_counts"]["blocked"] == 3
    assert result["test_counts"] == {"skipped": 1}


def test_branch_mapping_is_explicit_and_never_falls_back_to_main():
    meta = metadata()
    with pytest.raises(ValueError, match="mapping"):
        resolve_branch(meta, {})
    assert resolve_branch(meta, {"codex/feature": "intent/feature"}) == "intent/feature"
    assert resolve_branch({**meta, "git_branch": "main"}, {}) == "main"
    assert resolve_branch({**meta, "tiden_branch": "intent/specific"}, {}) == "intent/specific"


def cli_fixture(monkeypatch, tmp_path, *, actual_branch="branch-id", status="passed", attempts="7"):
    import subprocess
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        command = args[1:3]
        if command == ["branch", "list"]:
            value = {"branches": [{"id": "branch-id", "name": "intent/feature"}]}
        elif command == ["run", "create"]:
            value = {"seqNum": 3, "branchName": "intent/feature", "buildSha": "a" * 40}
        elif command == ["run", "get"]:
            value = {"status": status, "branchId": actual_branch, "liveDocStatus": "succeeded", "stats": {"attempts": attempts}}
        elif command == ["run", "complete"]:
            value = {"status": status}
        else:
            value = {}
        return subprocess.CompletedProcess(args, 1 if command == ["run", "complete"] and status == "failed" else 0,
                                           stdout=json.dumps(value), stderr="")
    monkeypatch.setattr(subprocess, "run", run)
    (tmp_path / "ci-checks.json").write_text("[]")
    manifest = {**metadata(), "schema": 1, "reports": ["ci-checks.json"], "test_counts": {},
                "check_counts": {"passed": 7}}
    return calls, manifest


def test_publisher_checks_the_final_resolved_branch_not_only_run_creation(tmp_path, monkeypatch):
    calls, manifest = cli_fixture(monkeypatch, tmp_path, actual_branch="wrong-branch")
    with pytest.raises(RuntimeError, match="expected branch"):
        publish(tmp_path, manifest, "intent/feature", "product")
    creation = next(call for call in calls if call[1:3] == ["run", "create"])
    assert creation[creation.index("--intent-session") + 1] == "none"


def test_publisher_refuses_partial_results_even_when_tiden_returns_passed(tmp_path, monkeypatch):
    _, manifest = cli_fixture(monkeypatch, tmp_path, attempts="1")
    with pytest.raises(RuntimeError, match="count differs"):
        publish(tmp_path, manifest, "intent/feature", "product")


def test_publisher_counts_additive_module_checks_without_changing_legacy_checks(tmp_path, monkeypatch):
    _, manifest = cli_fixture(monkeypatch, tmp_path, attempts=str(len(CHECKS) + len(MODULE_REPORTS)))
    manifest["module_checks"] = dict.fromkeys(MODULE_REPORTS, "success")
    manifest["check_counts"] = {"passed": len(CHECKS) + len(MODULE_REPORTS)}
    assert publish(tmp_path, manifest, "intent/feature", "product")["status"] == "passed"


def test_failed_completion_still_gets_a_verified_failure_receipt(tmp_path, monkeypatch):
    _, manifest = cli_fixture(monkeypatch, tmp_path, status="failed")
    manifest["check_counts"] = {"passed": 6, "failed": 1}
    assert publish(tmp_path, manifest, "intent/feature", "product")["status"] == "failed"


def test_publisher_refuses_a_green_verdict_for_blocked_ci(tmp_path, monkeypatch):
    _, manifest = cli_fixture(monkeypatch, tmp_path)
    manifest["check_counts"] = {"passed": 6, "blocked": 1}
    with pytest.raises(RuntimeError, match="contradicts"):
        publish(tmp_path, manifest, "intent/feature", "product")


def test_unmapped_feature_branch_is_routed_under_its_own_git_branch_name():
    meta = metadata()
    assert route_branch(meta, {}) == ("codex/feature", True)
    assert route_branch(meta, {"codex/feature": "intent/feature"}) == ("intent/feature", False)
    assert route_branch({**meta, "git_branch": "main"}, {}) == ("main", False)
    assert route_branch({**meta, "tiden_branch": "intent/specific"}, {}) == ("intent/specific", False)
    for name in ("Codex/Feature", "release/1.2", "codex//feature", "codex/feature/", "-feature"):
        with pytest.raises(ValueError, match="mapping"):
            route_branch({**meta, "git_branch": name}, {})


def routed_cli(monkeypatch, tmp_path, *, created="codex/feature", final_id="new-id",
               final_branches=({"id": "new-id", "name": "codex/feature"},), live_doc="succeeded",
               existing=(), creation="created"):
    import subprocess
    calls = []
    branches = [{"id": "main-id", "name": "main"}, *existing]
    def run(args, **kwargs):
        calls.append(args)
        command = args[1:3]
        if command == ["branch", "list"]:
            # Branches created while Tiden processes the run appear once the run exists.
            ran = any(call[1:3] == ["run", "create"] for call in calls)
            value = {"branches": [*branches, *(final_branches if ran else ())]}
        elif command == ["branch", "create"]:
            if creation != "refused":
                # "raced": a concurrent CI run for the same Git branch created it first.
                branches.append({"id": "new-id", "name": args[args.index("--name") + 1]})
            if creation != "created":
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="")
            value = branches[-1]
        elif command == ["run", "create"]:
            value = {"seqNum": 4, "branchName": created, "buildSha": "a" * 40}
        elif command == ["run", "get"]:
            value = {"status": "passed", "branchId": final_id, "liveDocStatus": live_doc, "stats": {"attempts": "7"}}
        elif command == ["run", "complete"]:
            value = {"status": "passed"}
        else:
            value = {}
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(value), stderr="")
    monkeypatch.setattr(subprocess, "run", run)
    (tmp_path / "ci-checks.json").write_text("[]")
    manifest = {**metadata(), "schema": 1, "reports": ["ci-checks.json"], "test_counts": {},
                "check_counts": {"passed": 7}}
    return calls, manifest


def test_routed_run_lands_on_the_same_named_branch_tiden_creates(tmp_path, monkeypatch):
    calls, manifest = routed_cli(monkeypatch, tmp_path)
    assert publish(tmp_path, manifest, "codex/feature", "product", routed=True)["branchId"] == "new-id"
    branch = next(call for call in calls if call[1:3] == ["branch", "create"])
    assert branch[branch.index("--name") + 1] == "codex/feature"
    creation = next(call for call in calls if call[1:3] == ["run", "create"])
    assert calls.index(branch) < calls.index(creation)
    assert creation[creation.index("--branch") + 1] == "codex/feature"
    assert creation[creation.index("--intent-session") + 1] == "none"


def test_routed_run_reuses_an_existing_same_named_branch(tmp_path, monkeypatch):
    calls, manifest = routed_cli(monkeypatch, tmp_path, final_id="old-id", final_branches=(),
                                 existing=({"id": "old-id", "name": "codex/feature"},))
    assert publish(tmp_path, manifest, "codex/feature", "product", routed=True)["branchId"] == "old-id"
    assert not any(call[1:3] == ["branch", "create"] for call in calls)


def test_routed_run_tolerates_a_concurrent_branch_creation(tmp_path, monkeypatch):
    _, manifest = routed_cli(monkeypatch, tmp_path, creation="raced")
    assert publish(tmp_path, manifest, "codex/feature", "product", routed=True)["branchId"] == "new-id"


def test_routed_run_refuses_when_no_tiden_branch_can_be_created(tmp_path, monkeypatch):
    calls, manifest = routed_cli(monkeypatch, tmp_path, creation="refused")
    with pytest.raises(ValueError, match="does not exist"):
        publish(tmp_path, manifest, "codex/feature", "product", routed=True)
    assert not any(call[1:3] == ["run", "create"] for call in calls)


def test_routed_run_follows_the_intent_branch_tiden_attributes_it_to(tmp_path, monkeypatch):
    _, manifest = routed_cli(monkeypatch, tmp_path, created="intent/2026-10-15-agents-feature", final_id="intent-id",
                             final_branches=({"id": "intent-id", "name": "intent/2026-10-15-agents-feature"},))
    assert publish(tmp_path, manifest, "codex/feature", "product", routed=True)["branchId"] == "intent-id"


@pytest.mark.parametrize("final_id, final_branches, live_doc", [
    ("main-id", (), "succeeded"),
    ("other-id", ({"id": "other-id", "name": "codex/other"},), "succeeded"),
    ("missing-id", (), "succeeded"),
    ("new-id", ({"id": "new-id", "name": "codex/feature"},), "failed"),
])
def test_routed_run_never_lands_on_main_or_an_unrelated_branch(tmp_path, monkeypatch, final_id, final_branches, live_doc):
    calls, manifest = routed_cli(monkeypatch, tmp_path, final_id=final_id, final_branches=final_branches, live_doc=live_doc)
    with pytest.raises(RuntimeError, match="expected branch"):
        publish(tmp_path, manifest, "codex/feature", "product", routed=True)
    assert not any(call[1:3] == ["run", "abort"] for call in calls)


def test_routed_run_refuses_a_rewritten_source_association(tmp_path, monkeypatch):
    calls, manifest = routed_cli(monkeypatch, tmp_path, created="main")
    with pytest.raises(RuntimeError, match="rewrote"):
        publish(tmp_path, manifest, "codex/feature", "product", routed=True)
    assert any(call[1:3] == ["run", "abort"] for call in calls)


# Synthetic Tiden identities; none of these belong to a real workspace or product.
WORKSPACE = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
PRODUCT = "11111111-2222-4333-8444-555555555555"
INTENT_ID = "99999999-8888-4777-8666-555555555555"
OTHER_ID = "77777777-8888-4777-8666-555555555555"
INTENT = {"id": INTENT_ID, "name": "intent/2026-10-16-feature", "status": "open"}


def tiden_link(branch=INTENT_ID, product=PRODUCT, workspace=WORKSPACE):
    return f"https://app.tiden.ai/{workspace}/products/{product}/branches/{branch}"


def intent_line(name=INTENT["name"], **link):
    return f"Tiden intent branch: [{name}]({tiden_link(**link)})"


def linked_manifest(manifest, *lines):
    return {**manifest, "pr_tiden_links": intent_links("\n".join(lines))}


def test_pull_request_description_yields_only_tiden_branch_ids():
    body = (f"Fix.\n\n{intent_line()}\n"
            f"Follow-up to [intent/2026-10-15-other]({tiden_link(OTHER_ID)}) and {tiden_link(OTHER_ID)}")
    assert intent_links(body) == [{"name": INTENT["name"], "workspace": WORKSPACE, "product": PRODUCT,
                                   "branch": INTENT_ID}]
    assert intent_links("") == [] and intent_links(None) == []
    many = "\n".join(intent_line(branch=f"{i:08d}-0000-4000-8000-000000000000") for i in range(9))
    assert len(intent_links(many)) == 5
    assert intent_links(intent_line(branch="not-a-uuid")) == []
    assert intent_links(intent_line(name="codex/feature")) == []


def test_linked_open_intent_branch_receives_pull_request_evidence(tmp_path, monkeypatch):
    calls, manifest = routed_cli(monkeypatch, tmp_path, created=INTENT["name"], final_id=INTENT_ID,
                                 final_branches=(), existing=(INTENT,))
    result = publish(tmp_path, linked_manifest(manifest, intent_line()), "codex/feature", PRODUCT,
                     routed=True, workspace=WORKSPACE)
    assert result["branchId"] == INTENT_ID
    assert not any(call[1:3] == ["branch", "create"] for call in calls)
    creation = next(call for call in calls if call[1:3] == ["run", "create"])
    assert creation[creation.index("--branch") + 1] == INTENT["name"]
    assert json.loads((tmp_path / "tiden-route.json").read_text()) == {
        "branch": INTENT["name"], "source": "pull-request-link"}


@pytest.mark.parametrize("existing, lines", [
    ((INTENT,), (intent_line(product="22222222-2222-4333-8444-555555555555"),)),
    ((INTENT,), (intent_line(workspace="bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee"),)),
    (({**INTENT, "status": "merged"},), (intent_line(),)),
    (({**INTENT, "name": "intent/2026-10-16-renamed"},), (intent_line(),)),
    ((INTENT, {"id": OTHER_ID, "name": "intent/2026-10-16-other", "status": "open"}),
     (intent_line(), intent_line(name="intent/2026-10-16-other", branch=OTHER_ID))),
    ((), (intent_line(),)),
], ids=["other-product", "other-workspace", "merged-branch", "text-names-another-branch",
        "two-intent-branches", "unknown-branch"])
def test_unusable_pull_request_link_keeps_git_branch_routing(tmp_path, monkeypatch, existing, lines):
    calls, manifest = routed_cli(monkeypatch, tmp_path, existing=existing)
    result = publish(tmp_path, linked_manifest(manifest, *lines), "codex/feature", PRODUCT,
                     routed=True, workspace=WORKSPACE)
    assert result["branchId"] == "new-id"
    creation = next(call for call in calls if call[1:3] == ["run", "create"])
    assert creation[creation.index("--branch") + 1] == "codex/feature"
    assert json.loads((tmp_path / "tiden-route.json").read_text())["source"] == "routed"


def test_explicit_target_wins_over_a_pull_request_link(tmp_path, monkeypatch):
    explicit = {"id": "explicit-id", "name": "intent/explicit", "status": "open"}
    calls, manifest = routed_cli(monkeypatch, tmp_path, created=explicit["name"], final_id="explicit-id",
                                 final_branches=(), existing=(explicit, INTENT))
    publish(tmp_path, linked_manifest(manifest, intent_line()), "intent/explicit", PRODUCT, workspace=WORKSPACE)
    creation = next(call for call in calls if call[1:3] == ["run", "create"])
    assert creation[creation.index("--branch") + 1] == "intent/explicit"
    meta = {**metadata(), "pr_tiden_links": intent_links(intent_line())}
    assert route_branch({**meta, "tiden_branch": "intent/explicit"}, {}) == ("intent/explicit", False)
    assert route_branch(meta, {"codex/feature": "intent/mapped"}) == ("intent/mapped", False)
    assert linked_intent_branch(meta["pr_tiden_links"], [INTENT], PRODUCT, WORKSPACE) == INTENT["name"]


def test_unroutable_git_branch_name_needs_a_usable_pull_request_link(tmp_path, monkeypatch):
    calls, manifest = routed_cli(monkeypatch, tmp_path, created=INTENT["name"], final_id=INTENT_ID,
                                 final_branches=(), existing=(INTENT,))
    manifest = {**linked_manifest(manifest, intent_line()), "git_branch": "Release/1.2"}
    assert publish(tmp_path, manifest, None, PRODUCT, routed=True, workspace=WORKSPACE)["branchId"] == INTENT_ID
    with pytest.raises(ValueError, match="intent link"):
        publish(tmp_path, {**manifest, "pr_tiden_links": []}, None, PRODUCT, routed=True, workspace=WORKSPACE)


def test_prepare_keeps_pull_request_link_ids_but_not_the_description(tmp_path, monkeypatch):
    import sys
    from scripts import tiden_ci
    (tmp_path / "metadata.json").write_text(json.dumps(metadata()))
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"body": f"Private note.\n{intent_line()}"}}))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setattr(sys, "argv", ["tiden_ci.py", "prepare", "--directory", str(tmp_path),
                                      "--source-root", str(tmp_path)])
    assert tiden_ci.main() == 0
    manifest = (tmp_path / "manifest.json").read_text()
    assert json.loads(manifest)["pr_tiden_links"] == [{"name": INTENT["name"], "workspace": WORKSPACE,
                                                       "product": PRODUCT, "branch": INTENT_ID}]
    assert "Private note" not in manifest
    assert "Private note" not in (tmp_path / "metadata.json").read_text()
    for absent in (None, str(tmp_path / "missing.json"), str(tmp_path / "metadata.json")):
        assert tiden_ci.pull_request_body(absent) == ""


def test_linked_run_attributed_to_another_branch_is_refused(tmp_path, monkeypatch):
    calls, manifest = routed_cli(monkeypatch, tmp_path, created=INTENT["name"], final_id="other-id",
                                 final_branches=({"id": "other-id", "name": "intent/2026-10-16-other"},),
                                 existing=(INTENT,))
    with pytest.raises(RuntimeError, match="expected branch"):
        publish(tmp_path, linked_manifest(manifest, intent_line()), "codex/feature", PRODUCT,
                routed=True, workspace=WORKSPACE)


def test_malformed_branch_map_fails_even_with_a_pull_request_link(tmp_path, monkeypatch):
    import sys
    from scripts import tiden_ci
    (tmp_path / "manifest.json").write_text(json.dumps({**metadata(), "schema": 1,
                                                        "pr_tiden_links": intent_links(intent_line())}))
    monkeypatch.setenv("TIDEN_BRANCH_MAP", '{"codex/feature": "intent/mapped",}')
    monkeypatch.setenv("TIDEN_PRODUCT_ID", PRODUCT)
    monkeypatch.setenv("TIDEN_WORKSPACE_ID", WORKSPACE)
    monkeypatch.setattr(sys, "argv", ["tiden_ci.py", "publish", "--directory", str(tmp_path)])
    published = []
    monkeypatch.setattr(tiden_ci, "publish", lambda *args, **kwargs: published.append(args))
    with pytest.raises(json.JSONDecodeError):
        tiden_ci.main()
    assert not published


def test_ci_keeps_the_description_out_of_the_environment_and_pushes_only_main():
    import yaml
    root = Path(__file__).resolve().parents[1]
    ci = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())
    trigger = ci.get("on", ci.get(True))
    assert trigger["push"] == {"branches": ["main"]}
    assert "pull_request" in trigger and "workflow_dispatch" in trigger
    for workflow in ("ci.yml", "checks.yml"):
        assert "pull_request.body" not in (root / ".github/workflows" / workflow).read_text()


def pull_request_manifest(manifest, *lines):
    return {**linked_manifest(manifest, *lines), "event": "pull_request"}


@pytest.mark.parametrize("existing, lines", [
    ((INTENT,), ()),
    ((INTENT,), (intent_line(product="22222222-2222-4333-8444-555555555555"),)),
    (({**INTENT, "status": "merged"},), (intent_line(),)),
    (({**INTENT, "name": "intent/2026-10-16-renamed"},), (intent_line(),)),
], ids=["no-link", "other-product", "merged-branch", "text-names-another-branch"])
def test_pull_request_without_usable_link_publishes_nothing(tmp_path, monkeypatch, capsys, existing, lines):
    calls, manifest = routed_cli(monkeypatch, tmp_path, existing=existing)
    result = publish(tmp_path, pull_request_manifest(manifest, *lines), "codex/feature", PRODUCT,
                     routed=True, workspace=WORKSPACE)
    assert result["status"] == "skipped" and result["branchName"] is None
    assert not any(call[1:3] in (["branch", "create"], ["run", "create"]) for call in calls)
    receipt = json.loads((tmp_path / "tiden-route.json").read_text())
    assert receipt["source"] == "skipped" and receipt["branch"] is None and "intent branch link" in receipt["reason"]
    out = capsys.readouterr().out
    if lines:
        # A link that stopped resolving is surfaced as a warning, not a quiet notice.
        assert "::warning title=Tiden evidence not published::" in out and "not usable" in receipt["reason"]
    else:
        # A pull request without a link needs no Tiden call at all.
        assert calls == [] and "::notice title=Tiden evidence not published::" in out


def test_linked_pull_request_still_publishes_to_its_intent_branch(tmp_path, monkeypatch):
    calls, manifest = routed_cli(monkeypatch, tmp_path, created=INTENT["name"], final_id=INTENT_ID,
                                 final_branches=(), existing=(INTENT,))
    result = publish(tmp_path, pull_request_manifest(manifest, intent_line()), "codex/feature", PRODUCT,
                     routed=True, workspace=WORKSPACE)
    assert result["branchId"] == INTENT_ID
    assert json.loads((tmp_path / "tiden-route.json").read_text())["source"] == "pull-request-link"


def test_non_pull_request_routing_is_unchanged_by_the_skip(tmp_path, monkeypatch):
    calls, manifest = routed_cli(monkeypatch, tmp_path)
    result = publish(tmp_path, {**manifest, "event": "workflow_dispatch"}, "codex/feature", PRODUCT,
                     routed=True, workspace=WORKSPACE)
    assert result["branchId"] == "new-id"
    assert any(call[1:3] == ["branch", "create"] for call in calls)
    calls, manifest = routed_cli(monkeypatch, tmp_path, created="main", final_id="main-id", final_branches=())
    assert publish(tmp_path, {**manifest, "event": "push", "git_branch": "main"}, "main", PRODUCT)["branchId"] == "main-id"


def test_unroutable_pull_request_branch_without_link_is_skipped(tmp_path, monkeypatch):
    import sys
    from scripts import tiden_ci
    calls, manifest = routed_cli(monkeypatch, tmp_path)
    (tmp_path / "manifest.json").write_text(json.dumps({**manifest, "event": "pull_request", "git_branch": "Release/1.2",
                                                        "pr_tiden_links": []}))
    monkeypatch.delenv("TIDEN_BRANCH_MAP", raising=False)
    monkeypatch.setenv("TIDEN_PRODUCT_ID", PRODUCT)
    monkeypatch.setenv("TIDEN_WORKSPACE_ID", WORKSPACE)
    monkeypatch.setattr(sys, "argv", ["tiden_ci.py", "publish", "--directory", str(tmp_path)])
    assert tiden_ci.main() == 0
    assert not any(call[1:3] in (["branch", "create"], ["run", "create"]) for call in calls)
    assert json.loads((tmp_path / "tiden-route.json").read_text())["source"] == "skipped"
    (tmp_path / "manifest.json").write_text(json.dumps({**manifest, "event": "workflow_dispatch",
                                                        "git_branch": "Release/1.2", "pr_tiden_links": []}))
    with pytest.raises(ValueError, match="mapping"):
        tiden_ci.main()


def test_prepare_records_the_ci_event_name(tmp_path, monkeypatch):
    import sys
    from scripts import tiden_ci
    (tmp_path / "metadata.json").write_text(json.dumps(metadata()))
    monkeypatch.delenv("GITHUB_EVENT_PATH", raising=False)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setattr(sys, "argv", ["tiden_ci.py", "prepare", "--directory", str(tmp_path),
                                      "--source-root", str(tmp_path)])
    assert tiden_ci.main() == 0
    assert json.loads((tmp_path / "manifest.json").read_text())["event"] == "pull_request"


def test_pull_request_branch_map_entry_still_publishes(tmp_path, monkeypatch):
    mapped = {"id": "mapped-id", "name": "intent/mapped", "status": "open"}
    calls, manifest = routed_cli(monkeypatch, tmp_path, created=mapped["name"], final_id="mapped-id",
                                 final_branches=(), existing=(mapped,))
    assert route_branch({**manifest, "event": "pull_request"}, {"codex/feature": "intent/mapped"}) == ("intent/mapped", False)
    result = publish(tmp_path, pull_request_manifest(manifest), "intent/mapped", PRODUCT, workspace=WORKSPACE)
    assert result["branchId"] == "mapped-id"
    creation = next(call for call in calls if call[1:3] == ["run", "create"])
    assert creation[creation.index("--branch") + 1] == "intent/mapped"
    assert json.loads((tmp_path / "tiden-route.json").read_text())["source"] == "explicit"


QUEUE_BRANCH = "gh-readonly-queue/main/pr-51-" + "b" * 40


@pytest.mark.parametrize("change, branch, routed", [
    ({"event": "merge_group", "git_branch": QUEUE_BRANCH}, QUEUE_BRANCH, True),
    ({"event": "workflow_dispatch", "git_branch": QUEUE_BRANCH}, QUEUE_BRANCH, True),
    ({"event": "merge_group", "git_branch": QUEUE_BRANCH, "tiden_branch": "intent/explicit",
      "pr_tiden_links": intent_links(intent_line())}, "intent/explicit", False),
    ({"event": "merge_group", "git_branch": "codex/feature"}, "codex/feature", True),
], ids=["merge-group", "queue-branch", "explicit-target-and-link", "merge-group-event-only"])
def test_merge_queue_run_publishes_nothing_to_tiden(tmp_path, monkeypatch, capsys, change, branch, routed):
    calls, manifest = routed_cli(monkeypatch, tmp_path, existing=(INTENT, {"id": "explicit-id", "name": "intent/explicit",
                                                                           "status": "open"}))
    result = publish(tmp_path, {**manifest, **change}, branch, PRODUCT, routed=routed, workspace=WORKSPACE)
    assert result["status"] == "skipped" and result["branchName"] is None and result["buildSha"] == "a" * 40
    # No branch listing, branch creation or run: the queue never touches Tiden.
    assert calls == []
    receipt = json.loads((tmp_path / "tiden-route.json").read_text())
    assert receipt["branch"] is None and receipt["source"] == "skipped" and "merge queue" in receipt["reason"]
    assert "::notice title=Tiden evidence not published::" in capsys.readouterr().out


def test_merge_queue_run_skips_before_the_branch_map_and_routing(tmp_path, monkeypatch, capsys):
    import sys
    from scripts import tiden_ci
    calls, _ = routed_cli(monkeypatch, tmp_path)
    # A base branch Tiden cannot name and a malformed map must not turn the queue red.
    queue_branch = "gh-readonly-queue/Release/1.2/pr-7-" + "b" * 40
    (tmp_path / "metadata.json").write_text(json.dumps({**metadata(), "git_branch": queue_branch}))
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"merge_group": {"head_sha": "a" * 40, "head_ref": "refs/heads/" + queue_branch}}))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_EVENT_NAME", "merge_group")
    monkeypatch.setattr(sys, "argv", ["tiden_ci.py", "prepare", "--directory", str(tmp_path),
                                      "--source-root", str(tmp_path)])
    assert tiden_ci.main() == 0
    assert json.loads((tmp_path / "manifest.json").read_text())["event"] == "merge_group"
    # The queue's reporting step runs without any Tiden credential or configuration.
    monkeypatch.setenv("TIDEN_BRANCH_MAP", '{"codex/feature": "intent/mapped",}')
    for name in ("TIDEN_API_TOKEN", "TIDEN_PRODUCT_ID", "TIDEN_WORKSPACE_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sys, "argv", ["tiden_ci.py", "publish", "--directory", str(tmp_path)])
    assert tiden_ci.main() == 0
    assert calls == []
    assert json.loads((tmp_path / "tiden-route.json").read_text())["source"] == "skipped"
    assert "::notice title=Tiden evidence not published::" in capsys.readouterr().out


def test_ci_reports_required_checks_for_merge_queue_runs_without_cancelling_them():
    import yaml
    root = Path(__file__).resolve().parents[1]
    ci = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())
    checks = yaml.safe_load((root / ".github/workflows/checks.yml").read_text())
    trigger = ci.get("on", ci.get(True))
    assert "merge_group" in trigger and trigger["push"] == {"branches": ["main"]}
    assert "pull_request" in trigger and "workflow_dispatch" in trigger
    # The required `CI gate` runs for every trigger, the queue included, and so does `checks / verify`, except
    # on a main push whose reuse job validated the merge queue's verification of that commit (ANYM-322) and on a
    # merge queue run that reused a full pull request run of its identical tree (ANYM-358).
    assert ci["jobs"]["gate"]["name"] == "CI gate" and ci["jobs"]["gate"]["if"] == "${{ always() }}"
    assert set(ci["jobs"]["gate"]["needs"]) == {"reuse", "pr_reuse", "checks", "native", "images"}
    assert ci["jobs"]["checks"]["uses"] == "./.github/workflows/checks.yml"
    assert "if" not in ci["jobs"]["images"]
    # native is skipped only with checks / verify on the queue's pull request reuse.
    assert ci["jobs"]["native"]["if"] == "${{ !cancelled() && needs.pr_reuse.outputs.decision != 'reuse' }}"
    assert ci["jobs"]["reuse"]["if"] == "github.event_name == 'push' && github.ref == 'refs/heads/main'"
    assert ci["jobs"]["checks"]["if"] == ("${{ !cancelled() && (needs.reuse.outputs.decision != 'reuse' "
                                          "|| needs.reuse.result == 'success') }}")
    assert ci["jobs"]["checks"]["with"]["reuse_queue_evidence"] == "${{ needs.reuse.outputs.decision == 'reuse' }}"
    assert checks.get("on", checks.get(True))["workflow_call"]["inputs"]["reuse_queue_evidence"]["default"] is False
    # It is skipped too on a pull request run of reviewed documentation only (ANYM-323).
    assert checks["jobs"]["verify"]["needs"] == "documentation"
    assert checks["jobs"]["verify"]["if"] == "${{ !cancelled() && !inputs.reuse_queue_evidence && !inputs.reuse_pr_verification && needs.documentation.outputs.docs_only != 'true' }}"
    step_ids = {step.get("id") for step in checks["jobs"]["verify"]["steps"]}
    assert set(CHECKS) <= step_ids
    # A queue run gets its own group and cancels nothing; so does a main push run.
    # Pull request and dispatch runs keep their groups.
    assert ci["concurrency"] == {
        "group": "ci-${{ github.event_name == 'merge_group' && format('merge-queue-{0}', github.run_id) "
                 "|| github.event_name == 'push' && format('main-push-{0}', github.run_id) "
                 "|| github.event.pull_request.number || github.ref }}",
        "cancel-in-progress": "${{ github.event_name != 'merge_group' && github.event_name != 'push' }}"}


def test_merge_queue_reporting_gets_no_tiden_credential_or_cli(tmp_path):
    import subprocess
    import yaml
    from scripts.tiden_ci import merge_queue_run
    root = Path(__file__).resolve().parents[1]
    ci = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())
    assert ci["jobs"]["checks"]["secrets"] == {
        "TIDEN_API_TOKEN": "${{ github.event_name != 'merge_group' && secrets.TIDEN_API_TOKEN || '' }}"}
    jobs = yaml.safe_load((root / ".github/workflows/checks.yml").read_text())["jobs"]
    job = jobs["tiden"]
    assert "github.event_name != 'pull_request'" in job["if"]  # queue runs still reach the reporting job
    steps = {step.get("name") or step["uses"].split("@")[0]: step for step in job["steps"]}
    config = steps["Check optional Tiden configuration"]
    # The job classifies the same event and Git branch that the evidence manifest records.
    prepared = next(step for step in jobs["verify"]["steps"] if step.get("name") == "Prepare honest CI evidence")
    assert config["env"]["SOURCE_BRANCH"] == prepared["env"]["SOURCE_BRANCH"] == "${{ github.head_ref || github.ref_name }}"
    assert config["env"]["EVENT_NAME"] == "${{ github.event_name }}"
    enabled_or_queue = "steps.config.outputs.enabled == 'true' || steps.config.outputs.queue == 'true'"
    assert steps["actions/checkout"]["if"] == enabled_or_queue
    # Only a configured non-queue run installs the CLI and receives the credential.
    for name in ("Install pinned Tiden CLI with integrity check", "Publish and verify branch association"):
        assert steps[name]["if"] == "steps.config.outputs.enabled == 'true'"
    queue = steps["Record that a merge queue run publishes nothing"]
    assert queue["if"] == "steps.config.outputs.queue == 'true'" and "env" not in queue
    assert queue["run"] == 'python scripts/tiden_ci.py queue-receipt --directory "$RUNNER_TEMP/ci-evidence"'
    assert f"({enabled_or_queue})" in steps["Retain Tiden association and verdict receipt"]["if"]

    def configure(**env):
        output, summary = tmp_path / "output", tmp_path / "summary"
        for path in (output, summary):
            path.write_text("")
        completed = subprocess.run(["bash", "-e", "-c", config["run"]], capture_output=True, text=True,
                                   env={"PATH": "/usr/bin:/bin", "GITHUB_OUTPUT": str(output),
                                        "GITHUB_STEP_SUMMARY": str(summary), **env})
        return completed.returncode, output.read_text().split(), summary.read_text()

    configured = {"TIDEN_API_TOKEN": "synthetic-token", "TIDEN_PRODUCT_ID": PRODUCT, "TIDEN_WORKSPACE_ID": WORKSPACE}
    queued = {"EVENT_NAME": "merge_group", "SOURCE_BRANCH": QUEUE_BRANCH}
    feature = {"EVENT_NAME": "pull_request", "SOURCE_BRANCH": "codex/feature"}
    # The queue path needs no credential, product or workspace, and never enables publication.
    assert configure(**queued) == (0, ["enabled=false", "queue=true"], "")
    assert configure(**queued, **configured)[1] == ["enabled=false", "queue=true"]
    assert configure(**feature, **configured) == (0, ["enabled=true"], "")
    unconfigured = configure(**feature)
    assert unconfigured[:2] == (0, ["enabled=false"]) and "not configured" in unconfigured[2]
    assert configure(**feature, TIDEN_API_TOKEN="synthetic-token", TIDEN_PRODUCT_ID=PRODUCT)[0] != 0
    # The job and scripts/tiden_ci.py agree on every run, including prefixes that differ only in case.
    for event, branch in [("merge_group", QUEUE_BRANCH), ("merge_group", "codex/feature"),
                          ("workflow_dispatch", QUEUE_BRANCH), ("pull_request", "GH-readonly-queue/feature"),
                          ("pull_request", "Gh-Readonly-Queue/main/pr-1"), ("push", "main"),
                          ("pull_request", "codex/gh-readonly-queue/x")]:
        queue = configure(EVENT_NAME=event, SOURCE_BRANCH=branch, **configured)[1] == ["enabled=false", "queue=true"]
        assert queue == merge_queue_run({"event": event, "git_branch": branch}), (event, branch)


# spec: ANYM-500
def test_a_merge_queue_run_records_its_skipped_receipt_without_downloading_the_evidence(tmp_path):
    import os
    import subprocess
    import yaml
    from scripts.tiden_ci import MERGE_QUEUE_SKIP
    root = Path(__file__).resolve().parents[1]
    job = yaml.safe_load((root / ".github/workflows/checks.yml").read_text())["jobs"]["tiden"]
    steps = {step.get("name") or step["uses"].split("@")[0]: step for step in job["steps"]}
    # A hung download of evidence that checks / evidence already enforced cannot fail a queue run.
    assert "queue" not in steps["actions/download-artifact"]["if"]
    queue = steps["Record that a merge queue run publishes nothing"]
    directory = tmp_path / "ci-evidence"  # absent, as without the download
    completed = subprocess.run(["bash", "-e", "-c", queue["run"]], cwd=root, capture_output=True, text=True,
                               env={"PATH": os.environ["PATH"], "RUNNER_TEMP": str(tmp_path)})
    assert completed.returncode == 0, completed.stderr
    assert json.loads((directory / "tiden-route.json").read_text()) == {
        "branch": None, "source": "skipped", "reason": MERGE_QUEUE_SKIP}
    assert "::notice title=Tiden evidence not published::" in completed.stdout
    assert [path.name for path in directory.iterdir()] == ["tiden-route.json"]


# spec: ANYM-500
def test_pull_request_and_push_runs_still_fail_without_their_evidence_artifact():
    import yaml
    root = Path(__file__).resolve().parents[1]
    job = yaml.safe_load((root / ".github/workflows/checks.yml").read_text())["jobs"]["tiden"]
    steps = {step.get("name") or step["uses"].split("@")[0]: step for step in job["steps"]}
    download = steps["actions/download-artifact"]
    # A run that publishes downloads its evidence, and a failed download still fails the job.
    assert download["if"] == "steps.config.outputs.enabled == 'true'" and download["with"]["name"] == "ci-evidence"
    assert "continue-on-error" not in download and "continue-on-error" not in job
    names = list(steps)
    assert names.index("actions/download-artifact") < names.index("Publish and verify branch association")


def image_workflow():
    import yaml
    root = Path(__file__).resolve().parents[1]
    jobs = yaml.safe_load((root / ".github/workflows/images.yml").read_text())["jobs"]
    decide = next(step for step in jobs["changes"]["steps"] if step.get("id") == "decide")
    return root, jobs, decide, [line for line in decide["env"]["IMAGE_INPUTS"].splitlines() if line]


def image_decision(cwd, tmp_path, event, head_sha=""):
    import subprocess
    _, _, decide, _ = image_workflow()
    output, summary = tmp_path / "output", tmp_path / "summary"
    for path in (output, summary):
        path.write_text("")
    # GitHub runs `run` scripts with `bash -e -o pipefail`.
    completed = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", decide["run"]], cwd=cwd, capture_output=True, text=True,
                               env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "GITHUB_OUTPUT": str(output),
                                    "GITHUB_STEP_SUMMARY": str(summary), "EVENT_NAME": event, "HEAD_SHA": head_sha,
                                    "IMAGE_INPUTS": decide["env"]["IMAGE_INPUTS"]})
    assert completed.returncode == 0, completed.stderr
    return output.read_text().split(), summary.read_text()


def pull_request_checkout(tmp_path, change, merge=True):
    """A shallow checkout of GitHub's synthetic merge commit for a pull request."""
    import subprocess

    def git(*args, cwd=tmp_path / "origin"):
        return subprocess.run(["git", "-c", "user.name=ci", "-c", "user.email=ci@example.invalid", "-c", "commit.gpgsign=false",
                               *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

    origin = tmp_path / "origin"
    for path in ("README.md", "LICENSE", "services/gateway/src/app.py", "deploy/compose.yaml", "docs/guide.md"):
        (origin / path).parent.mkdir(parents=True, exist_ok=True)
        (origin / path).write_text("base\n")
    git("init", "-q", "-b", "main")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    git("switch", "-q", "-c", "feature")
    change(origin, git)
    git("add", "-A")
    git("commit", "-q", "-m", "change")
    head = git("rev-parse", "HEAD")
    git("switch", "-q", "main")
    (origin / "docs/main.md").write_text("main moved on\n")
    git("add", "-A")
    git("commit", "-q", "-m", "main")
    git("merge", "-q", "--no-ff", "-m", "merge", "feature") if merge else git("reset", "-q", "--hard", "feature")
    git("clone", "-q", "--depth", "2", f"file://{origin}", str(tmp_path / "checkout"), cwd=tmp_path)
    return tmp_path / "checkout", head


@pytest.mark.parametrize("change, build", [
    (lambda root, git: (root / "README.md").write_text("docs\n"), False),
    (lambda root, git: [(root / name).write_text("new\n") for name in ("docs/guide.md", "docs/new.md")], False),
    # deploy/ is copied only into the test stage, which images.yml never builds.
    (lambda root, git: (root / "deploy/compose.yaml").write_text("changed\n"), False),
    (lambda root, git: (root / "services/gateway/src/app.py").write_text("changed\n"), True),
    (lambda root, git: git("mv", "services/gateway/src/app.py", "docs/app.py"), True),
    (lambda root, git: git("rm", "-q", "LICENSE"), True),
    # Git quotes these names in its default output; the decision must still see them.
    (lambda root, git: (root / "services/gateway/src/модуль.py").write_text("new\n"), True),
    (lambda root, git: (root / 'services/gateway/src/with"quote.py').write_text("new\n"), True),
    (lambda root, git: (root / "docs/заметки.md").write_text("new\n"), False),
], ids=["readme", "docs", "deploy", "service-source", "renamed-out-of-a-service", "deleted-license",
        "non-ascii-service-source", "quoted-service-source", "non-ascii-docs"])
def test_pull_request_builds_images_only_when_an_image_input_changes(tmp_path, change, build):
    checkout, head = pull_request_checkout(tmp_path, change)
    outputs, summary = image_decision(checkout, tmp_path, "pull_request", head)
    assert outputs == [f"build={str(build).lower()}"]
    assert ("image input changed:" in summary) == build and summary.startswith(f"Image build: {str(build).lower()}")


def test_image_build_runs_whenever_the_pull_request_diff_is_uncertain(tmp_path):
    docs_only = lambda root, git: (root / "README.md").write_text("docs\n")  # noqa: E731
    checkout, head = pull_request_checkout(tmp_path / "other-head", docs_only)
    assert image_decision(checkout, tmp_path, "pull_request", "0" * 40)[0] == ["build=true"]
    assert image_decision(checkout, tmp_path, "pull_request", "")[0] == ["build=true"]
    assert image_decision(checkout, tmp_path, "pull_request", head)[0] == ["build=false"]
    checkout, head = pull_request_checkout(tmp_path / "not-a-merge", docs_only, merge=False)
    outputs, summary = image_decision(checkout, tmp_path, "pull_request", head)
    assert outputs == ["build=true"] and "could not be established" in summary
    # Other events but the merge queue build without reading Git at all, even outside a checkout.
    for event in ("push", "workflow_dispatch"):
        empty = tmp_path / event
        empty.mkdir()
        assert image_decision(empty, tmp_path, event, head)[0] == ["build=true"]



# spec: ANYM-300
def test_a_merge_queue_run_never_builds_images_and_the_main_push_always_does(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    outputs, summary = image_decision(empty, tmp_path, "merge_group", "a" * 40)
    assert outputs == ["build=false"] and "merge queue runs never build" in summary
    assert image_decision(empty, tmp_path, "push")[0] == ["build=true"]

def test_image_build_job_is_gated_only_by_the_change_decision():
    import yaml
    root, jobs, decide, _ = image_workflow()
    ci = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())
    assert ci["jobs"]["images"] == {"uses": "./.github/workflows/images.yml",
                                    "with": {"source_ref": "${{ inputs.source_ref || '' }}"}}
    assert set(jobs) == {"changes", "build"}
    changes, build = jobs["changes"], jobs["build"]
    assert changes["outputs"] == {"build": "${{ steps.decide.outputs.build }}"} and "if" not in changes
    checkout = changes["steps"][0]
    assert checkout["uses"].startswith("actions/checkout@") and len(checkout["uses"].split("@")[1].split()[0]) == 40
    assert checkout["if"] == "${{ github.event_name == 'pull_request' }}"
    assert checkout["with"] == {"persist-credentials": False, "fetch-depth": 2}
    assert decide["env"]["EVENT_NAME"] == "${{ github.event_name }}"
    assert decide["env"]["HEAD_SHA"] == "${{ github.event.pull_request.head.sha }}"
    # A skipped build leaves the called workflow green; a failed decision or build turns it red,
    # and the CI gate requires `images` to succeed. Main push runs keep `images / build (<target>)`.
    assert build["needs"] == "changes" and build["if"] == "${{ needs.changes.outputs.build == 'true' }}"
    assert "name" not in build  # production.py finds `images / build (backup)` by the job key
    assert build["strategy"]["matrix"]["target"] == [
        "runtime", "indexer", "enricher-local", "whatsapp-native", "gateway", "calendar", "calls", "backup"]
    # The build and smoke test come first; the publication steps after it only export what was tested.
    names = [step.get("name") for step in build["steps"]]
    check = names.index("Build and smoke-test the production target without providers")
    assert build["steps"][check]["run"] == 'python3 scripts/production.py check-image --target "$TARGET"'
    # ANYM-441: the whatsmeow fork content check runs before check-image compiles the native adapter.
    assert names.index("Check the whatsmeow fork against upstream and fork.patch") < check
    assert all("docker build" not in step.get("run", "") for step in build["steps"])


def dockerfile_sources(dockerfile, stage):
    """Paths a target stage copies from its build context, following FROM and COPY --from stages."""
    import re
    stages, names = [], {}
    for line in re.sub(r"\\\n", " ", dockerfile.read_text()).splitlines():
        line = line.strip()
        if match := re.match(r"FROM\s+(?:--\S+\s+)*(\S+)(?:\s+AS\s+(\S+))?$", line, re.I):
            if match.group(2):
                names[match.group(2).lower()] = len(stages)
            stages.append({"parents": {match.group(1)}, "sources": []})
        elif stages and (match := re.match(r"(?:COPY|ADD)\s+(.+)$", line, re.I)):
            words = match.group(1).split()
            options = [word for word in words if word.startswith("--")]
            sources = [word for word in words if not word.startswith("--")][:-1]
            parent = [option.split("=", 1)[1] for option in options if option.startswith("--from=")]
            stages[-1]["parents"].update(parent)
            if not parent:
                stages[-1]["sources"].extend(sources)

    def index(reference):
        # A stage is referenced by name (case-insensitive) or by its zero-based position.
        return int(reference) if reference.isdigit() else names.get(reference.lower())

    pending, seen, sources = [len(stages) - 1 if not stage else index(stage)], set(), []
    while pending:
        number = pending.pop()
        if number is not None and number < len(stages) and number not in seen:
            seen.add(number)
            sources += stages[number]["sources"]
            pending += [index(parent) for parent in stages[number]["parents"]]
    return sources


def test_image_inputs_cover_every_source_the_image_targets_build_from(tmp_path):
    import importlib.util
    root, jobs, _, inputs = image_workflow()
    spec = importlib.util.spec_from_file_location("production_builds", root / "scripts/production.py")
    production = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(production)
    covered = lambda path: any(path == item.rstrip("/") or path.startswith(item) for item in inputs)  # noqa: E731
    assert {"scripts/production.py", ".github/workflows/images.yml", ".dockerignore"} <= set(inputs)
    for target in jobs["build"]["strategy"]["matrix"]["target"]:
        context, dockerfile, stage, _ = production.BUILDS[target]
        prefix = "" if context == "." else context.rstrip("/") + "/"
        assert covered(dockerfile) and covered(f"{dockerfile}.dockerignore"), target
        assert covered(prefix + ".dockerignore") or not (root / prefix / ".dockerignore").exists(), target
        sources = dockerfile_sources(root / dockerfile, stage)
        assert sources, target
        for source in sources:
            path = prefix + source.removeprefix("./").split("*")[0]
            assert covered(path), (target, source)
    # The parser follows only the stages a target needs: the test stage's deploy/ is not an input.
    assert "deploy" in dockerfile_sources(root / "services/messaging/Dockerfile", "test")
    assert not covered("deploy/compose.yaml") and not covered("docs/README.md")
    # Continued COPY lines and FROM options are parsed too, so a later Dockerfile edit cannot hide a source.
    (tmp_path / "Dockerfile").write_text("FROM --platform=linux/amd64 base AS build\nCOPY a \\\n    b ./\n"
                                         "FROM base AS Tools\nCOPY c ./\nFROM scratch\nCOPY d ./\n"
                                         "FROM build AS final\nCOPY --from=build /out /out\nCOPY --from=tools /t /t\n"
                                         "COPY --from=2 /s /s\n")
    assert sorted(dockerfile_sources(tmp_path / "Dockerfile", "final")) == ["a", "b", "c", "d"]
    assert dockerfile_sources(tmp_path / "Dockerfile", "") == dockerfile_sources(tmp_path / "Dockerfile", "final")


# spec: ANYM-300
def test_the_root_build_context_leaves_out_env_files_at_every_depth_but_keeps_the_examples():
    lines = [line.strip() for line in (Path(__file__).resolve().parents[1] / ".dockerignore").read_text().splitlines()]
    rules = [line for line in lines if line and not line.startswith("#")]
    for excluded in ("**/.env", "**/.env.*"):
        assert excluded in rules
    # Docker applies the last matching rule, so the example exception must come after the exclusions.
    assert rules.index("!**/.env.example") > rules.index("**/.env.*")



TRIAGE_FIXTURE = json.loads((Path(__file__).parent / "fixtures/tiden-run-triage-041.json").read_text())
TRIAGE_SENTINELS = ("SENTINEL-OUTPUT-542", "SENTINEL-ERROR-542")


def triage_cli(monkeypatch, tmp_path, status="failed", reply=None, mode=None):
    """cli_fixture's synthetic Tiden publishing run 3 with `status`, then answering `run triage` once."""
    import subprocess
    calls, manifest = cli_fixture(monkeypatch, tmp_path, status=status)
    synthetic = subprocess.run
    timeouts = []
    def run(args, **kwargs):
        if args[1:3] == ["run", "triage"]:
            calls.append(args)
            timeouts.append(kwargs["timeout"])
            if mode == "timeout":
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])
            if mode == "exit":
                return subprocess.CompletedProcess(args, 1, stdout=TRIAGE_SENTINELS[0], stderr=TRIAGE_SENTINELS[1])
            if mode == "malformed":
                return subprocess.CompletedProcess(args, 0, stdout="<html>" + TRIAGE_SENTINELS[0], stderr="")
            value = TRIAGE_FIXTURE["reply"] if reply is None else reply
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(value), stderr=TRIAGE_SENTINELS[1])
        completed = synthetic(args, **kwargs)
        if args[1:3] == ["run", "get"]:
            completed.stdout = json.dumps({**json.loads(completed.stdout), "seqNum": 3, "branchName": "intent/feature",
                                           "buildSha": "a" * 40})
        return completed
    monkeypatch.setattr(subprocess, "run", run)
    (tmp_path / "manifest.json").write_text(json.dumps({**manifest, "event": "push"}))
    return calls, timeouts


def run_publish(monkeypatch, tmp_path, branch_map=None):
    from scripts import tiden_ci
    monkeypatch.setenv("TIDEN_BRANCH_MAP", json.dumps({"codex/feature": "intent/feature"}
                                                      if branch_map is None else branch_map))
    monkeypatch.setenv("TIDEN_PRODUCT_ID", PRODUCT)
    monkeypatch.setenv("TIDEN_WORKSPACE_ID", WORKSPACE)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.setattr(sys, "argv", ["tiden_ci.py", "publish", "--directory", str(tmp_path)])
    return tiden_ci.main()


def publication_files(tmp_path):
    return {path.name: path.read_bytes() for path in tmp_path.glob("tiden-*.json") if path.name != "tiden-triage.json"}


# spec: ANYM-542
def test_a_failed_published_run_shows_its_failures_grouped_by_message(tmp_path, monkeypatch, capsys):
    reply = json.loads(json.dumps(TRIAGE_FIXTURE["reply"]))
    second = {**reply["clusters"][0], "count": 4, "message": "line one\nline\x1btwo " + "x" * 300,
              "failures": [{"title": "tests/test_a.py::test\x07one"}, {"title": "tests/test_a.py::test_two"}]}
    reply["clusters"].append(second)
    calls, timeouts = triage_cli(monkeypatch, tmp_path, reply=reply)
    # The publication's own result: run 3 failed, so the step fails as before.
    assert run_publish(monkeypatch, tmp_path) == 1
    triage = [call for call in calls if call[1:3] == ["run", "triage"]]
    assert triage == [["tiden", "run", "triage", "3", "--limit", "10", "--cluster-exemplars", "3",
                       "--failure-output-lines", "0", "--product-id", PRODUCT, "--format", "json"]]
    assert "--baseline" not in triage[0] and 0 < timeouts[0] <= 60
    # The triage comes after the publication's last call.
    assert calls.index(triage[0]) == len(calls) - 1
    lines = (tmp_path / "summary.md").read_text().splitlines()
    assert lines == ["Tiden triage: 1 × CI verification: python: GitHub Actions step outcome: cancelled",
                     "Tiden triage: 4 × tests/test_a.py::testone: " + ("line onelinetwo " + "x" * 300)[:200]]
    assert json.loads((tmp_path / "tiden-triage.json").read_text()) == {"triage": {"groups": [
        {"count": 1, "exemplar": "CI verification: python"}, {"count": 4, "exemplar": "tests/test_a.py::testone"}]}}
    captured = capsys.readouterr()
    assert json.loads(captured.out.splitlines()[-1])["status"] == "failed"
    assert not any(sentinel in text for text in (captured.out, captured.err, *lines) for sentinel in TRIAGE_SENTINELS)


# spec: ANYM-542
@pytest.mark.parametrize("status, event, branch_map, code", [
    ("passed", "push", None, 0),
    # An ordinary pull request without a mapping or an intent link is skipped before any Tiden run.
    ("failed", "pull_request", {}, 0),
])
def test_a_passed_or_skipped_publication_makes_no_triage_call(tmp_path, monkeypatch, status, event, branch_map, code):
    calls, _ = triage_cli(monkeypatch, tmp_path, status=status)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    (tmp_path / "manifest.json").write_text(json.dumps({**manifest, "event": event, "pr_tiden_links": []}))
    assert run_publish(monkeypatch, tmp_path, branch_map) == code
    assert not any(call[1:3] == ["run", "triage"] for call in calls)
    assert not (tmp_path / "tiden-triage.json").exists()


# spec: ANYM-542
@pytest.mark.parametrize("mode, reply, reason", [
    ("exit", None, "exit 1"),
    ("timeout", None, "timeout"),
    ("malformed", None, "malformed"),
    (None, {"clusters": [{"count": "1", "message": "m", "failures": [{"title": "t"}]}]}, "malformed"),
    (None, {"clusters": [{"count": 1, "message": "m", "failures": []}]}, "malformed"),
    (None, {"run": {}}, "malformed"),
])
def test_a_failed_triage_is_one_fixed_line_and_keeps_the_publication(tmp_path, monkeypatch, capsys, mode, reply,
                                                                     reason):
    calls, _ = triage_cli(monkeypatch, tmp_path, reply=reply, mode=mode)
    published = {}
    from scripts import tiden_ci
    original = tiden_ci.triage
    def recorded(directory, *arguments, **options):
        # The publication's result and receipts are already saved when the triage starts.
        published.update(publication_files(directory))
        return original(directory, *arguments, **options)
    monkeypatch.setattr(tiden_ci, "triage", recorded)
    assert run_publish(monkeypatch, tmp_path) == 1
    assert published and publication_files(tmp_path) == published
    assert (tmp_path / "summary.md").read_text() == f"Tiden triage: not available ({reason})\n"
    assert json.loads((tmp_path / "tiden-triage.json").read_text()) == {
        "triage": {"state": "not available", "reason": reason}}
    # Create, report and complete ran once each; nothing is repeated.
    assert Counter(tuple(call[1:3]) for call in calls)[("run", "complete")] == 1
    assert Counter(tuple(call[1:3]) for call in calls)[("run", "create")] == 1
    captured = capsys.readouterr()
    texts = [captured.out, captured.err, *(path.read_text() for path in tmp_path.glob("tiden-triage.json"))]
    assert not any(sentinel in text for text in texts for sentinel in TRIAGE_SENTINELS)


# spec: ANYM-542
@pytest.mark.parametrize("elapsed, timeout", [(0, 60), (100, 50), (139, 11), (140, 10)])
def test_the_triage_timeout_is_the_time_left_in_the_publish_step(tmp_path, monkeypatch, elapsed, timeout):
    from scripts.tiden_ci import triage
    calls, timeouts = triage_cli(monkeypatch, tmp_path)
    triage(tmp_path, 3, PRODUCT, 1000.0, clock=lambda: 1000.0 + elapsed)
    assert timeouts == [timeout]


# spec: ANYM-542
@pytest.mark.parametrize("elapsed", [140.5, 150, 400])
def test_with_under_ten_seconds_left_the_triage_is_skipped(tmp_path, monkeypatch, elapsed):
    from scripts.tiden_ci import triage
    calls, timeouts = triage_cli(monkeypatch, tmp_path)
    assert triage(tmp_path, 3, PRODUCT, 1000.0, clock=lambda: 1000.0 + elapsed) == [
        "Tiden triage: not available (no time)"]
    assert timeouts == [] and json.loads((tmp_path / "tiden-triage.json").read_text())["triage"]["reason"] == "no time"



# spec: ANYM-542
@pytest.mark.parametrize("elapsed", [140.5, 400])
def test_a_publication_without_time_left_skips_the_triage_and_keeps_its_result(tmp_path, monkeypatch, elapsed):
    calls, timeouts = triage_cli(monkeypatch, tmp_path)
    published, starts = {}, []
    from scripts import tiden_ci
    original = tiden_ci.triage
    def late(directory, seq, product, started, **options):
        # The publish step has already used `elapsed` seconds since the script started.
        published.update(publication_files(directory))
        starts.append(started)
        return original(directory, seq, product, started, clock=lambda: started + elapsed)
    monkeypatch.setattr(tiden_ci, "triage", late)
    import time
    before = time.monotonic()
    assert run_publish(monkeypatch, tmp_path) == 1
    # The budget counts from the script's start, taken in this run.
    assert len(starts) == 1 and before <= starts[0] <= time.monotonic()
    assert timeouts == [] and not any(call[1:3] == ["run", "triage"] for call in calls)
    assert published and publication_files(tmp_path) == published
    assert (tmp_path / "summary.md").read_text() == "Tiden triage: not available (no time)\n"


# spec: ANYM-542
def test_an_unwritable_triage_receipt_or_summary_keeps_the_publication_result(tmp_path, monkeypatch, capsys):
    calls, _ = triage_cli(monkeypatch, tmp_path)
    (tmp_path / "tiden-triage.json").mkdir()
    (tmp_path / "summary.md").mkdir()
    from scripts import tiden_ci
    original = tiden_ci.triage
    lines = []
    monkeypatch.setattr(tiden_ci, "triage", lambda *a, **k: lines.extend(original(*a, **k)) or lines)
    assert run_publish(monkeypatch, tmp_path) == 1
    assert lines[-1] == "Tiden triage: receipt not written"
    assert capsys.readouterr().out.splitlines()[-1] == "Tiden triage: summary not written"


RELEASE_SHA = "a" * 40
RUN_URL = "https://github.com/example/repo/actions/runs/7/attempts/1"
CREATED = "2026-01-01T00:10:00.000000Z"


def main_run(run_id="run-a", sha=RELEASE_SHA, status="passed", created="2026-01-01T00:00:00.000000Z",
             completed="2026-01-01T00:00:05.000000Z"):
    return {"id": run_id, "buildSha": sha, "status": status, "createdAt": created, "completedAt": completed}


def release_cli(monkeypatch, runs=None, *, created=CREATED, release=None,
                gate=(3, {"status": "VERDICT_STATUS_BLOCKED", "computedAt": "2026-01-01T00:10:01Z"})):
    """A synthetic Tiden: runs are listed newest-created first in pages; a release keeps its first creation time."""
    import subprocess
    runs = [main_run()] if runs is None else runs
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        command, code = args[1:3], 0
        if command == ["run", "list"]:
            size = int(args[args.index("--page-size") + 1])
            start = int(args[args.index("--page-token") + 1]) if "--page-token" in args else 0
            ordered = sorted(runs, key=lambda value: value["createdAt"], reverse=True)
            following = str(start + size) if start + size < len(ordered) else ""
            value = {"runs": ordered[start:start + size], "pagination": {"nextPageToken": following}}
        elif command == ["release", "create"]:
            version, environment = args[args.index("--version") + 1], args[args.index("--environment") + 1]
            value = release or {"id": "release-id", "version": version, "environmentSlug": environment,
                                "url": args[args.index("--url") + 1], "createdAt": created}
        elif command == ["gate", "check"]:
            code, value = gate
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, code, stdout=json.dumps(value) if value else "", stderr="")
    monkeypatch.setattr(subprocess, "run", run)
    return calls


def run_release(monkeypatch, tmp_path, *arguments):
    import sys
    from scripts import tiden_ci
    monkeypatch.setenv("TIDEN_PRODUCT_ID", PRODUCT)
    monkeypatch.setattr(sys, "argv", ["tiden_ci.py", "release", "--directory", str(tmp_path), *arguments])
    return tiden_ci.main()


PRODUCTION = ("--environment", "production", "--sha", RELEASE_SHA, "--url", RUN_URL,
              "--released-at", "2026-01-01T00:20:00Z")


def main_push_manifest(tmp_path, **change):
    manifest = {**metadata(), "git_branch": "main", "event": "push", "tiden_branch": "", "ci_url": RUN_URL,
                "schema": 1, **change}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))


def receipt(tmp_path):
    return json.loads((tmp_path / "tiden-release-receipt.json").read_text())


def test_main_push_run_records_its_exact_commit_release_after_publication(tmp_path, monkeypatch):
    calls = release_cli(monkeypatch, [main_run("run-old", "c" * 40, created="2025-12-31T23:00:00.000000Z",
                                               completed="2025-12-31T23:00:05.000000Z"), main_run()])
    main_push_manifest(tmp_path)
    assert run_release(monkeypatch, tmp_path, "--environment", "github-actions-synthetic") == 0
    lists = [call for call in calls if call[1:3] == ["run", "list"]]
    assert lists and all(call[call.index("--branch") + 1] == "main" and
                         call[call.index("--environment") + 1] == "github-actions-synthetic" for call in lists)
    create = next(call for call in calls if call[1:3] == ["release", "create"])
    # The gate is captured from main at creation, so creation follows the main-run check.
    assert calls[0][1:3] == ["run", "list"] and calls.index(create) > 0
    assert create[create.index("--version") + 1] == "deedkit@" + RELEASE_SHA
    assert create[create.index("--environment") + 1] == "github-actions-synthetic"
    assert create[create.index("--name") + 1] == "DeedKit " + RELEASE_SHA[:12]
    assert create[create.index("--url") + 1] == RUN_URL
    assert create[create.index("--product-id") + 1] == PRODUCT
    recorded = receipt(tmp_path)
    assert {key: recorded[key] for key in ("releaseId", "mainRun", "gateRun", "gateFromThisCommit", "gate")} == {
        "releaseId": "release-id", "mainRun": "run-a", "gateRun": "run-a", "gateFromThisCommit": True,
        "gate": "VERDICT_STATUS_BLOCKED"}


@pytest.mark.parametrize("change", [{"event": "pull_request"}, {"event": "merge_group"}, {"event": "workflow_dispatch"},
                                    {"git_branch": "codex/feature"}, {"tiden_branch": "intent/explicit"},
                                    {"git_branch": "Main"}])
def test_only_a_main_push_run_records_a_ci_release(tmp_path, monkeypatch, change):
    calls = release_cli(monkeypatch)
    main_push_manifest(tmp_path, **change)
    with pytest.raises(ValueError, match="main push"):
        run_release(monkeypatch, tmp_path, "--environment", "github-actions-synthetic")
    assert calls == []


def test_ci_release_takes_no_commit_url_or_time_from_arguments(tmp_path, monkeypatch):
    calls = release_cli(monkeypatch)
    main_push_manifest(tmp_path)
    with pytest.raises(ValueError, match="published run"):
        run_release(monkeypatch, tmp_path, "--environment", "github-actions-synthetic", "--sha", "b" * 40)
    assert calls == []


OTHER = "b" * 40


@pytest.mark.parametrize("latest", [
    [main_run(), main_run("run-b", OTHER, created="2026-01-01T00:00:01.000000Z", completed="2026-01-01T00:00:06.000000Z")],
    [main_run(status="failed")],
    [main_run(status="in_progress", completed=None)],
    [],
    # Created earlier but completed later: the latest completion, not the latest creation, decides.
    [main_run(), main_run("run-b", OTHER, created="2025-12-31T23:59:00.000000Z", completed="2026-01-01T00:00:09.000000Z")],
])
def test_release_is_not_recorded_unless_this_commit_passed_the_latest_main_run(tmp_path, monkeypatch, capsys, latest):
    calls = release_cli(monkeypatch, latest)
    main_push_manifest(tmp_path)
    # A newer main commit records its own CI release, so this run warns and keeps CI green.
    assert run_release(monkeypatch, tmp_path, "--environment", "github-actions-synthetic") == 0
    assert "Tiden release not recorded" in capsys.readouterr().out
    assert not any(call[1:3] == ["release", "create"] for call in calls)
    from scripts.tiden_ci import StaleRelease
    calls = release_cli(monkeypatch, latest)
    with pytest.raises(StaleRelease):
        run_release(monkeypatch, tmp_path, *PRODUCTION)
    assert not any(call[1:3] == ["release", "create"] for call in calls)


def filler(count, start="2026-01-01T00:01:00"):
    # Newer main runs of other commits that never completed (for example killed reporting jobs).
    base = datetime.fromisoformat(start)
    return [main_run(f"run-x{i}", OTHER, status="in_progress", completed=None,
                     created=(base + timedelta(seconds=i)).isoformat() + ".000000Z") for i in range(count)]


def test_latest_completion_is_found_beyond_the_first_page(tmp_path, monkeypatch):
    late = main_run("run-b", OTHER, created="2025-12-31T23:59:00.000000Z", completed="2026-01-01T00:00:09.000000Z")
    calls = release_cli(monkeypatch, [main_run(), late, *filler(60)])
    from scripts.tiden_ci import StaleRelease
    with pytest.raises(StaleRelease):
        run_release(monkeypatch, tmp_path, *PRODUCTION)
    assert any("--page-token" in call for call in calls)


def test_unbounded_run_listing_fails_closed(tmp_path, monkeypatch):
    from scripts import tiden_ci
    monkeypatch.setattr(tiden_ci, "MAX_RUN_PAGES", 1)
    calls = release_cli(monkeypatch, [main_run(), *filler(60)])
    with pytest.raises(RuntimeError, match="too many main runs"):
        run_release(monkeypatch, tmp_path, *PRODUCTION)
    assert not any(call[1:3] == ["release", "create"] for call in calls)


def test_newer_run_completed_during_creation_fails_the_release(tmp_path, monkeypatch):
    # Another commit's run completed after the check but before the creation captured the main gate.
    release_cli(monkeypatch, [main_run(), main_run("run-b", OTHER, created="2026-01-01T00:00:01.000000Z",
                                                   completed="2999-01-01T00:00:00.000000Z")],
                created="2999-01-01T00:05:00.000000Z")
    from scripts.tiden_ci import ForeignGate, record_release
    with pytest.raises(ForeignGate, match="run-b"):
        record_release(tmp_path, RELEASE_SHA, "production", RUN_URL, "2026-01-01T00:20:00Z", PRODUCT)
    assert receipt(tmp_path)["gateRun"] == "run-b" and receipt(tmp_path)["gateFromThisCommit"] is False


@pytest.mark.parametrize("runs, created", [
    # A rerun of this commit is latest now, but the release was first created while another run was latest.
    ([main_run("run-a2", created="2026-01-01T01:00:00.000000Z", completed="2026-01-01T01:00:05.000000Z"),
      main_run("run-b", OTHER, created="2026-01-01T00:00:01.000000Z", completed="2026-01-01T00:00:06.000000Z"),
      main_run()], "2026-01-01T00:10:00.000000Z"),
    # Created before Tiden could recompute the main gate from this run.
    ([main_run()], "2026-01-01T00:00:20.000000Z"),
])
def test_release_whose_gate_came_from_another_run_is_never_reported_as_this_commit(tmp_path, monkeypatch, capsys,
                                                                                    runs, created):
    from scripts.tiden_ci import ForeignGate
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    release_cli(monkeypatch, runs, created=created)
    with pytest.raises(ForeignGate):
        run_release(monkeypatch, tmp_path, *PRODUCTION)
    assert receipt(tmp_path)["gateFromThisCommit"] is False
    # A synthetic CI record is not promotion evidence: the run warns and keeps the finding in its receipt.
    release_cli(monkeypatch, runs, created=created)
    main_push_manifest(tmp_path)
    assert run_release(monkeypatch, tmp_path, "--environment", "github-actions-synthetic") == 0
    assert "gate from another run" in capsys.readouterr().out
    assert receipt(tmp_path)["gateFromThisCommit"] is False


def test_release_waits_for_the_main_gate_to_follow_a_just_completed_run(tmp_path, monkeypatch):
    from scripts import tiden_ci
    now = datetime.now(timezone.utc)
    def stamp(value):
        return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    slept = []
    monkeypatch.setattr(tiden_ci.time, "sleep", slept.append)
    release_cli(monkeypatch, [main_run(created=stamp(now - timedelta(seconds=15)),
                                       completed=stamp(now - timedelta(seconds=10)))],
                created=stamp(now + timedelta(seconds=60)))
    tiden_ci.record_release(tmp_path, RELEASE_SHA, "production", RUN_URL, "2026-01-01T00:20:00Z", PRODUCT)
    assert len(slept) == 1 and 45 <= slept[0] <= 50


def test_rewritten_release_identity_is_refused(tmp_path, monkeypatch):
    release_cli(monkeypatch, release={"id": "release-id", "version": "anymsg@0.9.19", "environmentSlug": "production",
                                      "url": RUN_URL, "createdAt": CREATED})
    from scripts.tiden_ci import record_release
    with pytest.raises(RuntimeError, match="rewrote"):
        record_release(tmp_path, RELEASE_SHA, "production", RUN_URL, "2026-01-01T00:20:00Z", PRODUCT)


def test_production_release_records_apply_time_and_reports_a_missing_gate(tmp_path, monkeypatch, capsys):
    calls = release_cli(monkeypatch, gate=(1, None))
    assert run_release(monkeypatch, tmp_path, *PRODUCTION) == 0
    create = next(call for call in calls if call[1:3] == ["release", "create"])
    assert create[create.index("--environment") + 1] == "production"
    assert create[create.index("--released-at") + 1] == "2026-01-01T00:20:00Z"
    assert "no gate was captured" in capsys.readouterr().out
    # Without a captured gate, there is no gate whose origin could be attributed.
    assert receipt(tmp_path)["gate"] is None and receipt(tmp_path)["gateFromThisCommit"] is None


@pytest.mark.parametrize("sha, environment, url, released_at", [
    ("A" * 40, "production", RUN_URL, "2026-10-17T00:00:30Z"),
    ("a" * 12, "production", RUN_URL, "2026-10-17T00:00:30Z"),
    (RELEASE_SHA, "staging", RUN_URL, "2026-10-17T00:00:30Z"),
    (RELEASE_SHA, "synthetic", RUN_URL, "2026-10-17T00:00:30Z"),
    (RELEASE_SHA, "production", "https://example.invalid/run/7", "2026-10-17T00:00:30Z"),
    (RELEASE_SHA, "production", RUN_URL + "/../../evil", "2026-10-17T00:00:30Z"),
    (RELEASE_SHA, "production", RUN_URL, "2026-10-17 00:00:30"),
    (RELEASE_SHA, "production", RUN_URL, "2026-10-17T00:00:30+03:00"),
    (RELEASE_SHA, "production", RUN_URL, "2999-01-01T00:00:00Z"),
])
def test_release_rejects_an_unverified_identity_before_any_tiden_call(tmp_path, monkeypatch, sha, environment, url,
                                                                       released_at):
    calls = release_cli(monkeypatch)
    from scripts.tiden_ci import record_release
    with pytest.raises(ValueError):
        record_release(tmp_path, sha, environment, url, released_at, PRODUCT)
    assert calls == []


def test_release_workflows_record_only_verified_main_commits(tmp_path):
    import subprocess
    import yaml
    root = Path(__file__).resolve().parents[1]
    checks = yaml.safe_load((root / ".github/workflows/checks.yml").read_text())["jobs"]
    # The seven Tiden check identities stay the verify job's step ids; evidence publication is unchanged.
    ids = [step.get("id") for step in checks["verify"]["steps"]]
    assert all(check in ids for check in CHECKS)
    assert "tiden_ci.py release" not in json.dumps(checks)
    ci = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())
    job = ci["jobs"]["release"]
    # Only after the CI gate and the image publication, so every verification job and the GHCR
    # publication passed; checks tells it whether the Tiden publication was verified.
    assert job["needs"] == ["gate", "publish", "checks"]
    assert job["if"] == ("${{ !cancelled() && needs.gate.result == 'success' && needs.publish.result == 'success' "
                         "&& github.event_name == 'push' && github.ref == 'refs/heads/main' }}")
    steps = {step.get("name") or step["uses"].split("@")[0]: step for step in job["steps"]}
    record = steps["Record the main release in Tiden"]
    assert record["if"] == "steps.config.outputs.enabled == 'true' && steps.download.outcome == 'success'"
    assert "continue-on-error" not in record
    assert record["run"] == ('python scripts/tiden_ci.py release --directory "$RUNNER_TEMP/ci-evidence" '
                             '--environment github-actions-synthetic')
    assert steps["actions/download-artifact"]["with"]["name"] == "ci-evidence"
    assert steps["actions/checkout"]["with"]["ref"] == "${{ github.sha }}"
    assert "release" not in ci["jobs"]["gate"]["needs"]

    workflow = yaml.safe_load((root / ".github/workflows/production-release.yml").read_text())
    trigger = workflow.get("on", workflow.get(True))
    assert list(trigger) == ["workflow_dispatch"]
    assert trigger["workflow_dispatch"]["inputs"]["environment"]["options"] == ["production"]
    assert workflow["permissions"] == {"contents": "read", "actions": "read"}
    job = workflow["jobs"]["record"]
    assert job["if"] == "github.ref == 'refs/heads/main'"
    steps = job["steps"]
    # The first step compares the ref with case, before any repository code runs.
    guard = steps[0]
    for ref, allowed in [("refs/heads/main", True), ("refs/heads/Main", False), ("refs/heads/MAIN", False),
                         ("refs/heads/feature", False)]:
        completed = subprocess.run(["bash", "-e", "-c", guard["run"]], env={"PATH": "/usr/bin:/bin", "REF": ref})
        assert (completed.returncode == 0) == allowed, ref
    assert guard["env"]["REF"] == "${{ github.ref }}"
    verify = next(i for i, step in enumerate(steps) if "production.py verify" in step.get("run", ""))
    publish = next(i for i, step in enumerate(steps) if "tiden_ci.py release" in step.get("run", ""))
    assert 0 < verify < publish
    # Every step up to the record runs only after the previous one succeeded.
    assert not any("if" in step or "continue-on-error" in step for step in steps[:publish + 1])
    # The Tiden token reaches only the release step and the advisory issues step after it, only from GitHub secrets.
    issues = next(i for i, step in enumerate(steps) if "tiden_ci.py release-issues" in step.get("run", ""))
    holders = [step for step in steps if "TIDEN_API_TOKEN" in json.dumps(step)]
    assert holders == [steps[publish], steps[issues]] and issues == publish + 1
    assert steps[issues]["env"]["TIDEN_API_TOKEN"] == "${{ secrets.TIDEN_API_TOKEN }}"
    assert "${{" not in steps[issues]["run"]
    assert steps[publish]["env"]["TIDEN_API_TOKEN"] == "${{ secrets.TIDEN_API_TOKEN }}"
    assert steps[publish]["env"]["ENVIRONMENT"] == "${{ inputs.environment }}"
    assert "${{" not in steps[verify]["run"] and "${{" not in steps[publish]["run"]


def unavailable_cli(monkeypatch, fail, mode, **options):
    """The synthetic Tiden of release_cli, unavailable at the `fail`-th call of one command: exit 1, garbage or a hang."""
    import subprocess
    calls = release_cli(monkeypatch, **options)
    synthetic, seen, timeouts = subprocess.run, Counter(), []
    def run(args, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        command, number = fail
        seen[tuple(args[1:3])] += 1
        if tuple(args[1:3]) == command and seen[command] == number:
            calls.append(args)
            if mode == "timeout":
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])
            return subprocess.CompletedProcess(args, 1 if mode == "exit" else 0,
                                               stdout="" if mode == "exit" else "<html>502</html>", stderr="")
        return synthetic(args, **kwargs)
    monkeypatch.setattr(subprocess, "run", run)
    return calls, timeouts


LISTING, CREATE, GATE = ("run", "list"), ("release", "create"), ("gate", "check")


# spec: ANYM-411
@pytest.mark.parametrize("mode", ["exit", "unparseable", "timeout"])
def test_an_unavailable_tiden_only_warns_in_the_ci_release_record_and_still_fails_production(tmp_path, monkeypatch,
                                                                                            capsys, mode):
    from scripts.tiden_ci import TidenUnavailable
    calls, _ = unavailable_cli(monkeypatch, (LISTING, 1), mode)
    main_push_manifest(tmp_path)
    assert run_release(monkeypatch, tmp_path, "--environment", "github-actions-synthetic") == 0
    out = capsys.readouterr().out
    assert out.startswith("::warning title=Tiden release not recorded::") and "TIDEN-CI.md" in out
    assert "receipt state not_attempted" in out and "ANYM-411" in out
    assert not any(call[1:3] == ["release", "create"] for call in calls)
    assert receipt(tmp_path) == {"state": "not_attempted", "version": "deedkit@" + RELEASE_SHA,
                                 "environment": "github-actions-synthetic", "url": RUN_URL,
                                 "releasedAt": receipt(tmp_path)["releasedAt"]}
    # The production record keeps failing on every error the CI record tolerates.
    unavailable_cli(monkeypatch, (LISTING, 1), mode)
    with pytest.raises(TidenUnavailable):
        run_release(monkeypatch, tmp_path, *PRODUCTION)
    assert receipt(tmp_path)["state"] == "not_attempted" and receipt(tmp_path)["environment"] == "production"


# spec: ANYM-411
@pytest.mark.parametrize("fail, mode, release_id", [
    ((LISTING, 2), "exit", "release-id"),  # the create succeeded, then the captured-run read failed
    ((LISTING, 2), "timeout", "release-id"),
    ((GATE, 1), "timeout", "release-id"),
    ((CREATE, 1), "timeout", None),  # the create was sent, but its response never came
    ((CREATE, 1), "unparseable", None),
    ((CREATE, 1), "exit", None),
], ids=["captured-read-exit", "captured-read-timeout", "gate-timeout", "create-timeout", "create-unparseable",
        "create-exit"])
def test_a_sent_release_create_is_never_retried_and_its_receipt_says_not_verified(tmp_path, monkeypatch, capsys,
                                                                                 fail, mode, release_id):
    from scripts.tiden_ci import TidenUnavailable
    calls, _ = unavailable_cli(monkeypatch, fail, mode)
    main_push_manifest(tmp_path)
    assert run_release(monkeypatch, tmp_path, "--environment", "github-actions-synthetic") == 0
    assert "receipt state not_verified" in capsys.readouterr().out
    assert sum(call[1:3] == ["release", "create"] for call in calls) == 1
    recorded = receipt(tmp_path)
    assert recorded["state"] == "not_verified" and recorded.get("releaseId") == release_id
    assert (recorded["version"], recorded["environment"], recorded["url"]) == (
        "deedkit@" + RELEASE_SHA, "github-actions-synthetic", RUN_URL)
    assert ("createdAt" in recorded) == bool(release_id) and "gateFromThisCommit" not in recorded
    calls, _ = unavailable_cli(monkeypatch, fail, mode)
    with pytest.raises(TidenUnavailable):
        run_release(monkeypatch, tmp_path, *PRODUCTION)
    assert sum(call[1:3] == ["release", "create"] for call in calls) == 1
    assert receipt(tmp_path)["state"] == "not_verified" and receipt(tmp_path).get("releaseId") == release_id


# spec: ANYM-411
def test_a_recorded_release_receipt_says_recorded(tmp_path, monkeypatch):
    release_cli(monkeypatch)
    main_push_manifest(tmp_path)
    assert run_release(monkeypatch, tmp_path, "--environment", "github-actions-synthetic") == 0
    assert receipt(tmp_path)["state"] == "recorded" and receipt(tmp_path)["releaseId"] == "release-id"


ISSUES_FIXTURE = json.loads((Path(__file__).parent / "fixtures/tiden-release-issues-041.json").read_text())
SENTINELS = ("SENTINEL-TITLE-540", "SENTINEL-MESSAGE-540")


def production_release(release_id, released_at=None, created=None, digit="b"):
    """One entry of a synthetic `release list --environment production` reply."""
    value = {"id": release_id, "version": "deedkit@" + digit * 40, "environmentSlug": "production"}
    return {**value, **({"releasedAt": released_at} if released_at else {}), **({"createdAt": created} if created else {})}


def recorded_production_release(monkeypatch, tmp_path):
    """The record step of production-release.yml, run first: its receipt bytes and Tiden calls."""
    calls = release_cli(monkeypatch)
    assert run_release(monkeypatch, tmp_path, *PRODUCTION) == 0
    return (tmp_path / "tiden-release-receipt.json").read_bytes(), calls


def issues_cli(monkeypatch, releases, reply=None, mode=None):
    """A synthetic Tiden for `release-issues`: a production release listing, then the issues of one release."""
    import subprocess
    reply = ISSUES_FIXTURE["reply"] if reply is None else reply
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        assert 0 < kwargs["timeout"] <= 45
        if args[1:3] == ["release", "list"]:
            value = {"releases": releases, "pagination": {"nextPageToken": ""}}
        elif args[1:3] == ["release", "issues"]:
            if mode == "timeout":
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])
            if mode == "exit":
                return subprocess.CompletedProcess(args, 2, stdout=SENTINELS[0], stderr=SENTINELS[1])
            if mode == "malformed":
                return subprocess.CompletedProcess(args, 0, stdout="<html>" + SENTINELS[0], stderr=SENTINELS[1])
            value = reply
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(value), stderr="")
    monkeypatch.setattr(subprocess, "run", run)
    return calls


def run_issues(monkeypatch, tmp_path, released_at="2026-01-01T00:20:00Z"):
    from scripts import tiden_ci
    monkeypatch.setenv("TIDEN_PRODUCT_ID", PRODUCT)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.setattr(sys, "argv", ["tiden_ci.py", "release-issues", "--directory", str(tmp_path),
                                      "--environment", "production", "--released-at", released_at])
    return tiden_ci.main()


def previous_issues(tmp_path):
    return json.loads((tmp_path / "tiden-previous-issues.json").read_text())


def assert_no_sentinel(tmp_path, captured):
    texts = [path.read_text() for path in tmp_path.glob("tiden-*.json")]
    texts += [captured.out, captured.err, (tmp_path / "summary.md").read_text()]
    assert not any(sentinel in text for text in texts for sentinel in SENTINELS)


# spec: ANYM-540
def test_a_recorded_production_release_lists_the_ids_first_seen_in_the_previous_release(tmp_path, monkeypatch,
                                                                                         capsys):
    before, record_calls = recorded_production_release(monkeypatch, tmp_path)
    # A reply with title and message text: only the projection of it is ever written.
    reply = json.loads(json.dumps(ISSUES_FIXTURE["reply"]))
    reply["newIssues"][0]["title"], reply["newIssues"][0]["message"] = SENTINELS
    releases = [production_release("release-id", "2026-01-01T00:20:00Z", digit="c"),  # this release: excluded
                production_release("older", "2026-01-01T00:05:00Z", digit="a"),
                production_release("previous", "2026-01-01T00:10:00Z"),
                production_release("later", "2026-01-01T00:30:00Z", digit="d")]
    calls = issues_cli(monkeypatch, releases, reply)
    capsys.readouterr()
    assert run_issues(monkeypatch, tmp_path) == 0
    ids = [issue["id"] for issue in ISSUES_FIXTURE["reply"]["newIssues"]]
    assert len(ids) == 7
    assert previous_issues(tmp_path) == {"state": "listed",
                                         "predecessor": {"id": "previous", "version": "deedkit@" + "b" * 40},
                                         "newIssues": ids, "seenCount": int(ISSUES_FIXTURE["reply"]["seenCount"])}
    line = "first seen in deedkit@bbbbbbbbbbbb: 7 issues: " + ", ".join(ids)
    captured = capsys.readouterr()
    assert captured.out == line + "\n" and (tmp_path / "summary.md").read_text() == line + "\n"
    assert [call[1:4] for call in calls] == [["release", "list", "--environment"], ["release", "issues", "previous"]]
    assert calls[0][4] == "production" and all(call[-4:] == ["--product-id", PRODUCT, "--format", "json"]
                                               for call in calls)
    assert_no_sentinel(tmp_path, captured)
    # The record's receipt and calls are untouched by the advisory step.
    assert (tmp_path / "tiden-release-receipt.json").read_bytes() == before
    assert all(call[1:3] in (["run", "list"], ["release", "create"], ["gate", "check"]) for call in record_calls)


# spec: ANYM-540
def test_a_previous_release_without_new_issues_lists_none(tmp_path, monkeypatch, capsys):
    before, _ = recorded_production_release(monkeypatch, tmp_path)
    issues_cli(monkeypatch, [production_release("previous", "2026-01-01T00:10:00Z")],
               {"newIssues": [], "seenCount": "0"})
    capsys.readouterr()
    assert run_issues(monkeypatch, tmp_path) == 0
    assert previous_issues(tmp_path)["newIssues"] == [] and previous_issues(tmp_path)["seenCount"] == 0
    assert capsys.readouterr().out == "first seen in deedkit@bbbbbbbbbbbb: 0 issues\n"
    assert (tmp_path / "tiden-release-receipt.json").read_bytes() == before


# spec: ANYM-540
@pytest.mark.parametrize("releases, state", [
    # Only this release, or only later ones: nothing earlier to read.
    ([production_release("release-id", "2026-01-01T00:20:00Z")], "no predecessor"),
    ([production_release("later", "2026-01-01T00:30:00Z")], "no predecessor"),
    # Two releases at the same newest earlier time.
    ([production_release("one", "2026-01-01T00:10:00Z"), production_release("two", "2026-01-01T00:10:00Z")],
     "ambiguous predecessor"),
    # Another release at this release's own time.
    ([production_release("previous", "2026-01-01T00:10:00Z"), production_release("twin", "2026-01-01T00:20:00Z")],
     "ambiguous predecessor"),
    # A release without `releasedAt` that is not provably older than the chosen one.
    ([production_release("previous", "2026-01-01T00:10:00Z"),
      production_release("undated", created="2026-01-01T00:12:00Z")], "ambiguous predecessor"),
    ([production_release("previous", "2026-01-01T00:10:00Z"), production_release("undated")],
     "ambiguous predecessor"),
    ([production_release("undated", created="2026-01-01T00:01:00Z")], "ambiguous predecessor"),
])
def test_no_or_an_ambiguous_predecessor_makes_no_issues_call(tmp_path, monkeypatch, capsys, releases, state):
    before, _ = recorded_production_release(monkeypatch, tmp_path)
    calls = issues_cli(monkeypatch, releases)
    capsys.readouterr()
    assert run_issues(monkeypatch, tmp_path) == 0
    assert previous_issues(tmp_path) == {"state": state, "predecessor": None, "newIssues": [], "seenCount": None}
    assert capsys.readouterr().out == f"Tiden previous-release issues: none listed ({state})\n"
    assert [call[1:3] for call in calls] == [["release", "list"]]
    assert (tmp_path / "tiden-release-receipt.json").read_bytes() == before


# spec: ANYM-540
def test_an_undated_release_created_before_the_predecessor_is_still_ambiguous(tmp_path, monkeypatch):
    # A repeated create keeps `createdAt` but moves the release time, so `createdAt` bounds nothing.
    recorded_production_release(monkeypatch, tmp_path)
    calls = issues_cli(monkeypatch, [production_release("previous", "2026-01-01T00:10:00Z"),
                                     production_release("undated", created="2026-01-01T00:09:00Z")])
    assert run_issues(monkeypatch, tmp_path) == 0
    assert previous_issues(tmp_path)["state"] == "ambiguous predecessor"
    assert [call[1:3] for call in calls] == [["release", "list"]]


# spec: ANYM-540
@pytest.mark.parametrize("mode, reply, reason", [
    ("exit", None, "exit 2"),
    ("timeout", None, "timeout"),
    ("malformed", None, "malformed"),
    (None, {"newIssues": [{"id": "has space", "title": SENTINELS[0]}], "seenCount": "1"}, "malformed"),
    (None, {"newIssues": [], "seenCount": SENTINELS[1]}, "malformed"),
    (None, {"issues": []}, "malformed"),
])
def test_a_failed_issues_read_writes_a_fixed_reason_and_keeps_the_record(tmp_path, monkeypatch, capsys, mode, reply,
                                                                          reason):
    before, _ = recorded_production_release(monkeypatch, tmp_path)
    issues_cli(monkeypatch, [production_release("previous", "2026-01-01T00:10:00Z")], reply, mode)
    capsys.readouterr()
    # Only this continue-on-error step fails, so the run shows it.
    assert run_issues(monkeypatch, tmp_path) == 1
    assert previous_issues(tmp_path) == {"state": "unavailable", "reason": reason}
    captured = capsys.readouterr()
    assert captured.out == f"Tiden previous-release issues: unavailable ({reason})\n"
    assert_no_sentinel(tmp_path, captured)
    assert (tmp_path / "tiden-release-receipt.json").read_bytes() == before


# spec: ANYM-540
@pytest.mark.parametrize("listing", [
    "<html>" + SENTINELS[0],
    json.dumps({"releases": [{**production_release("previous", "2026-01-01T00:10:00Z"), "version": SENTINELS[0] + " x"}],
                "pagination": {}}),
])
def test_an_unreadable_release_listing_is_malformed(tmp_path, monkeypatch, capsys, listing):
    import subprocess
    recorded_production_release(monkeypatch, tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda args, **kwargs: subprocess.CompletedProcess(args, 0, stdout=listing,
                                                                                              stderr=""))
    capsys.readouterr()
    assert run_issues(monkeypatch, tmp_path) == 1
    assert previous_issues(tmp_path) == {"state": "unavailable", "reason": "malformed"}
    assert_no_sentinel(tmp_path, capsys.readouterr())


# spec: ANYM-540
def test_a_listing_with_more_pages_cannot_prove_the_predecessor(tmp_path, monkeypatch, capsys):
    import subprocess
    recorded_production_release(monkeypatch, tmp_path)
    listing = {"releases": [production_release("previous", "2026-01-01T00:10:00Z")],
               "pagination": {"nextPageToken": "2"}}
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(listing), stderr="")
    monkeypatch.setattr(subprocess, "run", run)
    capsys.readouterr()
    assert run_issues(monkeypatch, tmp_path) == 1
    assert previous_issues(tmp_path) == {"state": "unavailable", "reason": "more pages"}
    assert capsys.readouterr().out == "Tiden previous-release issues: unavailable (more pages)\n"
    assert [call[1:3] for call in calls] == [["release", "list"]]


# spec: ANYM-540
@pytest.mark.parametrize("releases, reply", [
    # A timestamp without a zone cannot be compared with the UTC release time.
    ([production_release("previous", "2026-01-01T00:10:00")], None),
    ([production_release("previous", "2026-01-01T00:10:00Z"), {**production_release("other"), "releasedAt": 5}],
     None),
    ([production_release("previous", "2026-01-01T00:10:00Z"), production_release("other", SENTINELS[0])], None),
    # A count too long to be a count.
    ([production_release("previous", "2026-01-01T00:10:00Z")], {"newIssues": [], "seenCount": "9" * 5000}),
])
def test_malformed_timestamps_and_counts_write_a_fixed_reason(tmp_path, monkeypatch, capsys, releases, reply):
    before, _ = recorded_production_release(monkeypatch, tmp_path)
    issues_cli(monkeypatch, releases, reply)
    capsys.readouterr()
    assert run_issues(monkeypatch, tmp_path) == 1
    assert previous_issues(tmp_path) == {"state": "unavailable", "reason": "malformed"}
    captured = capsys.readouterr()
    assert captured.out == "Tiden previous-release issues: unavailable (malformed)\n"
    assert_no_sentinel(tmp_path, captured)
    assert (tmp_path / "tiden-release-receipt.json").read_bytes() == before


# spec: ANYM-540
@pytest.mark.parametrize("state", [None, "not_verified"])
def test_no_recorded_release_means_no_tiden_call(tmp_path, monkeypatch, state):
    if state:
        (tmp_path / "tiden-release-receipt.json").write_text(json.dumps({"state": state, "releaseId": "release-id"}))
    calls = issues_cli(monkeypatch, [production_release("previous", "2026-01-01T00:10:00Z")])
    assert run_issues(monkeypatch, tmp_path) == 1
    assert previous_issues(tmp_path) == {"state": "unavailable", "reason": "no recorded release"} and calls == []


# spec: ANYM-540
def test_the_issues_read_ends_by_its_own_deadline(tmp_path, monkeypatch):
    import time
    from scripts.tiden_ci import release_issues
    recorded_production_release(monkeypatch, tmp_path)
    calls = issues_cli(monkeypatch, [production_release("previous", "2026-01-01T00:10:00Z")])
    result = release_issues(tmp_path, "2026-01-01T00:20:00Z", PRODUCT, deadline=time.monotonic() - 1)
    assert result == {"state": "unavailable", "reason": "timeout"} and calls == []


# spec: ANYM-540
def test_the_issues_step_is_advisory_and_runs_only_after_a_recorded_production_release():
    import yaml
    root = Path(__file__).resolve().parents[1]
    job = yaml.safe_load((root / ".github/workflows/production-release.yml").read_text())["jobs"]["record"]
    names = [step.get("name") for step in job["steps"]]
    step = job["steps"][names.index("List the issues first seen in the previous production release")]
    assert names.index("Record the production release in Tiden") + 1 == names.index(step["name"])
    assert names.index(step["name"]) + 1 == names.index("Retain the release receipt")
    assert job["steps"][names.index("Record the production release in Tiden")]["id"] == "record"
    assert step["if"] == "steps.record.outcome == 'success' && inputs.environment == 'production'"
    assert step["continue-on-error"] is True and step["timeout-minutes"] == 1 and job["timeout-minutes"] == 6
    assert "--environment production" in step["run"] and '"$RUNNER_TEMP/release"' in step["run"]


# spec: ANYM-411
def test_the_ci_release_record_ends_every_call_and_the_settle_wait_by_its_deadline(tmp_path, monkeypatch):
    import yaml
    from scripts import tiden_ci
    root = Path(__file__).resolve().parents[1]
    job = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())["jobs"]["release"]
    steps = {step.get("name") or step["uses"].split("@")[0]: step for step in job["steps"]}
    # Every step has its own timeout and together, with a minute of headroom, they stay below the job's, so the
    # warning and the receipt upload always run; the script ends its own work 30 seconds before its step's timeout.
    assert all("timeout-minutes" in step for step in steps.values())
    assert sum(step["timeout-minutes"] for step in steps.values()) + 1 < job["timeout-minutes"]
    assert tiden_ci.CI_RELEASE_DEADLINE <= steps["Record the main release in Tiden"]["timeout-minutes"] * 60 - 30
    assert tiden_ci.CLI_TIMEOUT == 120

    clock = [1000.0]
    monkeypatch.setattr(tiden_ci.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(tiden_ci.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    now = datetime.now(timezone.utc)
    def stamp(value):
        return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    # The run just completed, so the gate settles in about 50 seconds, but only 20 are left.
    runs = [main_run(created=stamp(now - timedelta(seconds=15)), completed=stamp(now - timedelta(seconds=10)))]
    calls, timeouts = unavailable_cli(monkeypatch, (GATE, 99), "exit", runs=runs)
    with pytest.raises(tiden_ci.TidenUnavailable, match="deadline"):
        tiden_ci.record_release(tmp_path, RELEASE_SHA, "github-actions-synthetic", RUN_URL, "2026-01-01T00:20:00Z",
                                PRODUCT, deadline=1020.0)
    assert clock[0] == 1020.0 and timeouts == [20.0]
    assert not any(call[1:3] == ["release", "create"] for call in calls)
    assert receipt(tmp_path)["state"] == "not_attempted"
    # Each call's timeout is the smaller of 120 seconds and the time left.
    clock[0] = 1000.0
    calls, timeouts = unavailable_cli(monkeypatch, (GATE, 99), "exit")
    tiden_ci.record_release(tmp_path, RELEASE_SHA, "github-actions-synthetic", RUN_URL, "2026-01-01T00:20:00Z",
                            PRODUCT, deadline=1090.0)
    assert timeouts and all(timeout == 90.0 for timeout in timeouts)
    # Production keeps no deadline: each call keeps its 120-second timeout.
    _, timeouts = unavailable_cli(monkeypatch, (GATE, 99), "exit")
    assert run_release(monkeypatch, tmp_path, *PRODUCTION) == 0
    assert timeouts and all(timeout == 120 for timeout in timeouts)


FAKE_TIDEN = """#!{python}
import json, os, sys
scenario, args = os.environ["FAKE_TIDEN"], sys.argv[1:3]
if os.environ.get("FAKE_TIDEN_LOG"):
    with open(os.environ["FAKE_TIDEN_LOG"], "a") as log:
        log.write(" ".join(args) + "\\n")
if scenario == "exit":
    sys.exit(1)
if scenario == "unparseable":
    print("<html>502 Bad Gateway</html>")
    sys.exit(0)
if args == ["run", "list"]:
    run = {{"id": "run-a", "buildSha": "{sha}", "status": "passed", "createdAt": "2026-01-01T00:00:00.000000Z",
            "completedAt": "2026-01-01T00:00:05.000000Z"}}
    print(json.dumps({{"listing": [run]}} if scenario == "unexpected" else {{"runs": [run], "pagination": {{}}}}))
elif args == ["release", "create"]:
    print(json.dumps({{"id": "release-id", "version": "anymsg@0.9.19", "environmentSlug": "github-actions-synthetic",
                      "url": "{url}", "createdAt": "2026-01-01T00:10:00.000000Z"}}))
else:
    sys.exit(2)
"""


# spec: ANYM-411
def test_the_release_job_tolerates_only_an_unavailable_tiden(tmp_path):
    import os
    import subprocess
    import tarfile
    import yaml
    root = Path(__file__).resolve().parents[1]
    job = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())["jobs"]["release"]
    steps = {step.get("name") or step["uses"].split("@")[0]: step for step in job["steps"]}
    names = list(steps)
    download, install = "Download pinned Tiden CLI", "Install pinned Tiden CLI with integrity check"
    warn, record = "Warn that the Tiden CLI was not downloaded", "Record the main release in Tiden"
    # Only the download may fail without failing the job, and it only downloads; no job-level suppression.
    assert [name for name, step in steps.items() if step.get("continue-on-error")] == [download]
    assert "continue-on-error" not in job and steps[download]["id"] == "download"
    assert "curl " in steps[download]["run"] and "sha256sum" not in steps[download]["run"] \
        and "tar " not in steps[download]["run"]
    assert names.index(download) < names.index(install) < names.index(warn) < names.index(record)
    assert steps[install]["if"] == steps[record]["if"] == (
        "steps.config.outputs.enabled == 'true' && steps.download.outcome == 'success'")
    assert steps[warn]["if"] == "steps.config.outputs.enabled == 'true' && steps.download.outcome != 'success'"
    script = steps[install]["run"]
    assert script.index("sha256sum --check") < script.index("tar -xzf")

    def bash(step, temp, **env):
        temp.mkdir(exist_ok=True)
        return subprocess.run(["bash", "-e", "-c", steps[step]["run"]], capture_output=True, text=True, env={
            "PATH": os.environ["PATH"], "RUNNER_TEMP": str(temp), "GITHUB_PATH": str(temp / "path"),
            "GITHUB_WORKSPACE": str(root),
            "GITHUB_OUTPUT": str(temp / "output"), "GITHUB_STEP_SUMMARY": str(temp / "summary"), **env})

    # A failed download warns with a receipt that records no create call.
    completed = bash(warn, tmp_path / "download", DOWNLOAD="failure", SHA=RELEASE_SHA)
    assert completed.returncode == 0
    assert completed.stdout.startswith("::warning title=Tiden release not recorded::the Tiden CLI download ended "
                                       "failure (receipt state not_attempted)") and "TIDEN-CI.md" in completed.stdout
    assert json.loads((tmp_path / "download" / "ci-evidence" / "tiden-release-receipt.json").read_text()) == {
        "state": "not_attempted", "version": "deedkit@" + RELEASE_SHA, "environment": "github-actions-synthetic",
        "download": "failure"}
    # A missing product or workspace ID with a token set fails the job.
    for product, workspace in (("", "workspace"), ("product", ""), ("", "")):
        completed = bash("Check optional Tiden configuration", tmp_path / f"config-{product}-{workspace}",
                         TIDEN_API_TOKEN="synthetic-token", TIDEN_PRODUCT_ID=product, TIDEN_WORKSPACE_ID=workspace,
                         TIDEN_PUBLISHED="true")
        assert completed.returncode != 0, (product, workspace)
    # A checksum mismatch fails the job, and the downloaded CLI is never extracted or put on the path.
    tampered = tmp_path / "tampered"
    tampered.mkdir()
    (tampered / "tiden").write_text("#!/bin/sh\nexit 0\n")
    with tarfile.open(tampered / "tiden.tar.gz", "w:gz") as archive:
        archive.add(tampered / "tiden", arcname="tiden")
    assert bash(install, tampered).returncode != 0
    assert not (tampered / "tiden-bin").exists() and not (tampered / "path").exists()

    # The record step itself, with a synthetic Tiden CLI on the path.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "tiden").write_text(FAKE_TIDEN.format(python=sys.executable, sha=RELEASE_SHA, url=RUN_URL))
    (bin_dir / "tiden").chmod(0o755)
    path = os.pathsep.join([str(bin_dir), str(Path(sys.executable).parent), "/usr/bin", "/bin"])

    def recorded(scenario, product=PRODUCT, **change):
        temp = tmp_path / f"record-{scenario}-{bool(product)}-{bool(change)}"
        (temp / "ci-evidence").mkdir(parents=True)
        main_push_manifest(temp / "ci-evidence", **change)
        completed = subprocess.run(["bash", "-e", "-c", steps[record]["run"]], capture_output=True, text=True,
                                   cwd=root, env={"PATH": path, "RUNNER_TEMP": str(temp), "FAKE_TIDEN": scenario,
                                                  "TIDEN_API_TOKEN": "synthetic-token", "TIDEN_PRODUCT_ID": product,
                                                  "TIDEN_WORKSPACE_ID": "workspace"})
        return completed, temp / "ci-evidence"

    for scenario in ("exit", "unparseable"):
        completed, directory = recorded(scenario)
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.startswith("::warning title=Tiden release not recorded::")
        assert json.loads((directory / "tiden-release-receipt.json").read_text())["state"] == "not_attempted"
    # Still fatal: a rewritten release identity, an unexpected response, a non-main-push manifest, no product ID.
    for scenario, product, change, error in (("rewritten", PRODUCT, {}, "rewrote the release identity"),
                                             ("unexpected", PRODUCT, {}, "KeyError"),
                                             ("exit", PRODUCT, {"event": "pull_request"}, "main push"),
                                             ("exit", "", {}, "TIDEN_PRODUCT_ID")):
        completed, _ = recorded(scenario, product, **change)
        assert completed.returncode != 0 and error in completed.stderr, (scenario, completed.stderr)
        assert "::warning" not in completed.stdout


# spec: ANYM-411
def test_the_release_entrypoints_fail_on_invalid_input_before_any_tiden_call(tmp_path):
    import os
    import subprocess
    import yaml
    root = Path(__file__).resolve().parents[1]
    ci = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())["jobs"]["release"]
    ci_step = next(step for step in ci["steps"] if step.get("name") == "Record the main release in Tiden")
    production = yaml.safe_load((root / ".github/workflows/production-release.yml").read_text())["jobs"]["record"]
    production_step = next(step for step in production["steps"] if "tiden_ci.py release" in step.get("run", ""))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "tiden").write_text(FAKE_TIDEN.format(python=sys.executable, sha=RELEASE_SHA, url=RUN_URL))
    (bin_dir / "tiden").chmod(0o755)
    path = os.pathsep.join([str(bin_dir), str(Path(sys.executable).parent), "/usr/bin", "/bin"])

    def step(script, name, **env):
        temp = tmp_path / name
        temp.mkdir()
        log = temp / "tiden-calls"
        completed = subprocess.run(["bash", "-e", "-c", script], capture_output=True, text=True, cwd=root, env={
            "PATH": path, "RUNNER_TEMP": str(temp), "FAKE_TIDEN": "exit", "FAKE_TIDEN_LOG": str(log),
            "TIDEN_API_TOKEN": "synthetic-token", "TIDEN_PRODUCT_ID": PRODUCT, "TIDEN_WORKSPACE_ID": "workspace", **env})
        # An exit-1 Tiden would only warn on the CI path, so a failure here comes from validation before any call.
        assert completed.returncode != 0 and not log.exists() and "::warning" not in completed.stdout, (
            name, completed.stdout, completed.stderr)
        return temp

    for name, change in (("upper-sha", {"sha": "A" * 40}), ("short-sha", {"sha": "a" * 12}),
                         ("foreign-url", {"ci_url": "https://example.invalid/run/7"}),
                         ("escaping-url", {"ci_url": RUN_URL + "/../../evil"})):
        evidence = tmp_path / "evidence" / name
        evidence.mkdir(parents=True)
        main_push_manifest(evidence, **change)
        directory = step(f'cp -R "{evidence}" "$RUNNER_TEMP/ci-evidence"\n' + ci_step["run"], name)
        assert not (directory / "ci-evidence" / "tiden-release-receipt.json").exists()
    production_inputs = {"SHA": RELEASE_SHA, "RUN_URL": RUN_URL, "RELEASED_AT": "2026-01-01T00:20:00Z",
                         "ENVIRONMENT": "production"}
    for name, change in (("production-upper-sha", {"SHA": "A" * 40}), ("production-short-sha", {"SHA": "a" * 12}),
                         ("production-foreign-url", {"RUN_URL": "https://example.invalid/run/7"}),
                         ("production-local-time", {"RELEASED_AT": "2026-10-17T00:00:30+03:00"}),
                         ("production-spaced-time", {"RELEASED_AT": "2026-10-17 00:00:30"}),
                         ("production-future-time", {"RELEASED_AT": "2999-01-01T00:00:00Z"}),
                         ("production-staging", {"ENVIRONMENT": "staging"}),
                         ("production-synthetic", {"ENVIRONMENT": "synthetic"})):
        step(production_step["run"], name, **{**production_inputs, **change})


# spec: ANYM-411
@pytest.mark.parametrize("code, stdout", [(0, "<html>502</html>"), (1, '{"error": "unavailable"}'), (0, "[]"), (0, ""),
                                          (3, '{"computedAt": "2026-01-01T00:10:01Z"}')],
                         ids=["garbage", "error-object", "list", "empty", "verdict-without-status"])
def test_a_gate_check_answer_that_is_neither_a_verdict_nor_a_missing_gate_means_tiden_was_unavailable(
        tmp_path, monkeypatch, capsys, code, stdout):
    import subprocess
    from scripts.tiden_ci import TidenUnavailable

    def answering(monkeypatch):
        calls = release_cli(monkeypatch)
        synthetic = subprocess.run
        def run(args, **kwargs):
            if args[1:3] == ["gate", "check"]:
                calls.append(args)
                return subprocess.CompletedProcess(args, code, stdout=stdout, stderr="")
            return synthetic(args, **kwargs)
        monkeypatch.setattr(subprocess, "run", run)
        return calls

    calls = answering(monkeypatch)
    main_push_manifest(tmp_path)
    assert run_release(monkeypatch, tmp_path, "--environment", "github-actions-synthetic") == 0
    out = capsys.readouterr().out
    assert "Tiden release not recorded" in out and "receipt state not_verified" in out
    assert "no gate was captured" not in out
    assert sum(call[1:3] == ["release", "create"] for call in calls) == 1
    assert {key: receipt(tmp_path).get(key) for key in ("state", "releaseId", "createdAt")} == {
        "state": "not_verified", "releaseId": "release-id", "createdAt": CREATED}
    answering(monkeypatch)
    with pytest.raises(TidenUnavailable):
        run_release(monkeypatch, tmp_path, *PRODUCTION)
    assert receipt(tmp_path)["state"] == "not_verified"


# spec: ANYM-411
def test_a_gate_check_that_fails_printing_json_null_is_no_missing_gate(tmp_path, monkeypatch, capsys):
    import subprocess
    from scripts.tiden_ci import TidenUnavailable

    def answering(monkeypatch):
        calls = release_cli(monkeypatch)
        synthetic = subprocess.run
        def run(args, **kwargs):
            if args[1:3] == ["gate", "check"]:
                return subprocess.CompletedProcess(args, 1, stdout="null", stderr="synthetic unavailable")
            return synthetic(args, **kwargs)
        monkeypatch.setattr(subprocess, "run", run)
        return calls

    # Only empty output means a missing gate; a printed JSON null is no answer.
    calls = answering(monkeypatch)
    main_push_manifest(tmp_path)
    assert run_release(monkeypatch, tmp_path, "--environment", "github-actions-synthetic") == 0
    out = capsys.readouterr().out
    assert "receipt state not_verified" in out and "no gate was captured" not in out
    assert sum(call[1:3] == ["release", "create"] for call in calls) == 1
    assert {key: receipt(tmp_path).get(key) for key in ("state", "releaseId", "createdAt")} == {
        "state": "not_verified", "releaseId": "release-id", "createdAt": CREATED}
    calls = answering(monkeypatch)
    with pytest.raises(TidenUnavailable):
        run_release(monkeypatch, tmp_path, *PRODUCTION)
    assert sum(call[1:3] == ["release", "create"] for call in calls) == 1
    assert receipt(tmp_path)["state"] == "not_verified"


# spec: ANYM-411
def test_the_receipt_is_current_before_the_create_call_is_sent(tmp_path, monkeypatch):
    import subprocess
    release_cli(monkeypatch)
    synthetic = subprocess.run
    def run(args, **kwargs):
        if args[1:3] == ["release", "create"]:
            # The process dies while the create is in flight: whatever the receipt says now is what is left.
            assert receipt(tmp_path)["state"] == "not_verified" and "releaseId" not in receipt(tmp_path)
            raise OSError("killed")
        assert receipt(tmp_path)["state"] == "not_attempted"
        return synthetic(args, **kwargs)
    monkeypatch.setattr(subprocess, "run", run)
    main_push_manifest(tmp_path)
    with pytest.raises(OSError, match="killed"):
        run_release(monkeypatch, tmp_path, "--environment", "github-actions-synthetic")
    assert receipt(tmp_path)["state"] == "not_verified"


# spec: ANYM-411
def test_the_overall_deadline_ends_a_hang_outside_tiden_calls_and_fails_the_ci_record(tmp_path, monkeypatch):
    import signal
    import time as clock
    from scripts import tiden_ci
    assert 0 < tiden_ci.CI_RECORD_MARGIN < tiden_ci.CI_RELEASE_DEADLINE
    # The Tiden calls get the overall deadline less the margin.
    seen = {}
    def recorded(*arguments, deadline: float, **options):
        seen["left"] = deadline - clock.monotonic()
        return {"state": "recorded"}
    monkeypatch.setattr(tiden_ci, "record_release", recorded)
    main_push_manifest(tmp_path)
    assert run_release(monkeypatch, tmp_path, "--environment", "github-actions-synthetic") == 0
    budget = tiden_ci.CI_RELEASE_DEADLINE - tiden_ci.CI_RECORD_MARGIN
    assert budget - 5 < seen["left"] <= budget
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    monkeypatch.undo()

    # A hang in manifest validation (a pull request chain reads GitHub) is ended by the overall deadline: the job
    # fails, the receipt says no create was sent, and the timer and handler are restored.
    calls = release_cli(monkeypatch)
    monkeypatch.setattr(tiden_ci, "CI_RELEASE_DEADLINE", 0.3)
    monkeypatch.setattr(tiden_ci, "published_manifest", lambda directory, manifest: clock.sleep(5))
    handler = signal.getsignal(signal.SIGALRM)
    started = clock.monotonic()
    with pytest.raises(tiden_ci.RecordDeadline):
        run_release(monkeypatch, tmp_path, "--environment", "github-actions-synthetic")
    assert clock.monotonic() - started < 3 and calls == []
    assert receipt(tmp_path) == {"state": "not_attempted"}
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0) and signal.getsignal(signal.SIGALRM) == handler


ATTEMPT_URL ="https://github.com/example/repo/actions/runs/1/attempts/2"
LOAD_REPORT = '<testsuite><testcase name="budgets" file="tests/platform/benchmarks/gateway_read_load.py"/></testsuite>'
PASSED = {"verify": ("success", "a" * 40), "load": ("success", "a" * 40), **dict.fromkeys(SHARD_JOBS, ("success", "a" * 40)),
          "go": ("success", "a" * 40)}
# Synthetic Go modules at the module directories of the two Go checks; `go_results` reads only go.mod and *_test.go.
GO_MODULE_PATHS = {"whatsmeow": "example.com/whatsmeow", "release_router": "example.com/release/router"}


def go_module(root, check, source="func TestPasses(t *testing.T) {}\n", file="x_test.go"):
    """Write a synthetic Go module for `check` under `root` (one test file); return its module path."""
    from scripts.tiden_ci import MODULE_REPORTS
    directory = root / MODULE_REPORTS[check][2]
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "go.mod").write_text(f"module {GO_MODULE_PATHS[check]}\n\ngo 1.26\n")
    (directory / file).write_text('package x\n\nimport "testing"\n\n' + source)
    return GO_MODULE_PATHS[check]


def go_stream(*events):
    return "".join((event if isinstance(event, str) else json.dumps(event)) + "\n" for event in events)


def passing_go_stream(module, test="TestPasses"):
    return go_stream({"Action": "start", "Package": module}, {"Action": "run", "Package": module, "Test": test},
                     {"Action": "output", "Package": module, "Test": test, "Output": f"=== RUN   {test}\n"},
                     {"Action": "pass", "Package": module, "Test": test, "Elapsed": 0.01},
                     {"Action": "pass", "Package": module, "Elapsed": 0.02})


def shard_report(job):
    return f'<testsuites name="pytest tests"><testsuite name="pytest"><testcase name="{job}" file="tests/{job}.py"/></testsuite></testsuites>'


def evidence_fragments(tmp_path, *, load_sha="a" * 40, load_outcomes=None, load=True, load_attempt="2",
                       verify_modules=None, verify_files=(), load_files=(), shards=SHARD_JOBS, go=True, go_streams=None):
    fragments = tmp_path / "fragments"
    verify = fragments / "ci-evidence-verify"
    verify.mkdir(parents=True)
    (verify / "source-sha").write_text("a" * 40 + "\n")
    (verify / "attempt").write_text("2\n")
    modules = verify_modules or dict.fromkeys(FRAGMENTS["verify"][0], "success")
    (verify / "metadata.json").write_text(json.dumps({**metadata(), "tiden_branch": "", "module_checks": modules}))
    for name in ("python-1.xml", *verify_files):
        (verify / name).write_text('<testsuite><testcase name="x" file="tests/x.py"/></testsuite>')
    for job in shards:  # the `checks / python` matrix jobs, shards 2-6 of the root suite
        fragment = fragments / f"ci-evidence-{job}"
        fragment.mkdir()
        (fragment / "source-sha").write_text("a" * 40 + "\n")
        (fragment / "attempt").write_text("2\n")
        (fragment / f"{job}.xml").write_text(shard_report(job))
        (fragment / "outcomes.json").write_text(json.dumps({"checks": {"python": "success"}}))
    if load:
        fragment = fragments / "ci-evidence-load"
        fragment.mkdir()
        (fragment / "source-sha").write_text(load_sha + "\n")
        (fragment / "attempt").write_text(load_attempt + "\n")
        for name in ("load.xml", *load_files):
            (fragment / name).write_text(LOAD_REPORT)
        (fragment / "load.json").write_text('{"status": "passed"}')
        (fragment / "outcomes.json").write_text(json.dumps(load_outcomes or {"module_checks": {"load": "success"}}))
    if go:  # the `go` job: one `go test -json` stream per Go module, of modules under the prepare root
        fragment = fragments / "ci-evidence-go"
        fragment.mkdir()
        (fragment / "source-sha").write_text("a" * 40 + "\n")
        (fragment / "attempt").write_text("2\n")
        for check, name in (("whatsmeow", "whatsmeow.json"), ("release_router", "release-router.json")):
            (fragment / name).write_text((go_streams or {}).get(check) or passing_go_stream(go_module(tmp_path, check)))
        (fragment / "outcomes.json").write_text(json.dumps({"module_checks": {"whatsmeow": "success", "release_router": "success"}}))
    return fragments


def assembled_rows(tmp_path, merged):
    out = tmp_path / "ci-evidence"
    prepare(tmp_path, out, json.loads((out / "metadata.json").read_text()))
    return {row["signature"]: row["execution"]["status"] for row in json.loads((out / "ci-checks.json").read_text())}


def test_parallel_load_fragment_is_assembled_with_verify_evidence_of_the_same_commit(tmp_path):
    from scripts.tiden_ci import assemble
    out = tmp_path / "ci-evidence"
    merged = assemble(out, evidence_fragments(tmp_path), ATTEMPT_URL, PASSED)
    # The assembling attempt names the evidence, so a rerun of one test job publishes under new row identities.
    assert merged["ci_url"] == ATTEMPT_URL and merged["fragments"] == ["verify", "load", *SHARD_JOBS, "go"] and merged["missing"] == []
    assert merged["module_checks"]["load"] == "success" and set(merged["module_checks"]) == set(MODULE_REPORTS)
    assert {path.name for path in out.iterdir()} == {"metadata.json", "source-sha", "python.xml", "load.xml", "load.json",
                                                     "whatsmeow.json", "release-router.json"}
    # The shard reports merge into the one python report, in shard order, so test identities do not change.
    assert [case.get("name") for case in ET.parse(out / "python.xml").getroot().iter("testcase")] == ["x", *SHARD_JOBS]
    rows = assembled_rows(tmp_path, merged)
    assert rows["deedkit-ci::load"] == rows["anymsg-ci::python"] == rows["deedkit-ci::whatsmeow"] == "passed"
    # A job that succeeded and was not rerun keeps its earlier attempt's fragment, and the evidence says so.
    earlier = assemble(tmp_path / "earlier", evidence_fragments(tmp_path / "e", load_attempt="1"), ATTEMPT_URL, PASSED)
    assert earlier["fragments"] == ["verify", "load", *SHARD_JOBS, "go"]
    assert earlier["producers"] == {"verify": {"result": "success", "sha": "a" * 40, "attempt": "2"},
                                    "load": {"result": "success", "sha": "a" * 40, "attempt": "1"},
                                    **{job: {"result": "success", "sha": "a" * 40, "attempt": "2"} for job in (*SHARD_JOBS, "go")}}
    # Assembling again into a used directory could keep a report the new fragments no longer supply.
    with pytest.raises(ValueError, match="new or empty"):
        assemble(out, evidence_fragments(tmp_path / "again", load=False), ATTEMPT_URL, PASSED)


def test_assembly_refuses_a_fragment_of_another_commit_or_of_foreign_checks(tmp_path):
    from scripts.tiden_ci import assemble
    with pytest.raises(ValueError, match="another source"):
        assemble(tmp_path / "a", evidence_fragments(tmp_path / "sha", load_sha="b" * 40), ATTEMPT_URL, PASSED)
    with pytest.raises(ValueError, match="another source"):  # the job's captured checkout must be verify's too
        assemble(tmp_path / "b", evidence_fragments(tmp_path / "captured"), ATTEMPT_URL,
                 {**PASSED, "load": ("success", "b" * 40)})
    foreign = {"module_checks": {"load": "success", "gateway": "success"}}
    with pytest.raises(ValueError, match="only its own"):
        assemble(tmp_path / "c", evidence_fragments(tmp_path / "foreign", load_outcomes=foreign), ATTEMPT_URL, PASSED)
    with pytest.raises(ValueError, match="only its own"):
        assemble(tmp_path / "d", evidence_fragments(tmp_path / "python", load_outcomes={"checks": {"python": "success"}}),
                 ATTEMPT_URL, PASSED)
    # Neither job can supply the other's checks or reports: verify claiming the load gate, load supplying pytest.
    claimed = {**dict.fromkeys(FRAGMENTS["verify"][0], "success"), "load": "success"}
    with pytest.raises(ValueError, match="only its own"):
        assemble(tmp_path / "e", evidence_fragments(tmp_path / "claim", load=False, verify_modules=claimed), ATTEMPT_URL, PASSED)
    with pytest.raises(ValueError, match="does not produce"):
        assemble(tmp_path / "f", evidence_fragments(tmp_path / "vload", load=False, verify_files=("load.xml",)), ATTEMPT_URL, PASSED)
    with pytest.raises(ValueError, match="does not produce"):
        assemble(tmp_path / "g", evidence_fragments(tmp_path / "lpy", load_files=("python.xml",)), ATTEMPT_URL, PASSED)
    without_verify = evidence_fragments(tmp_path / "verify")
    for path in (without_verify / "ci-evidence-verify").iterdir():
        path.unlink()
    with pytest.raises(ValueError, match="verify fragment"):
        assemble(tmp_path / "h", without_verify, ATTEMPT_URL, PASSED)
    with pytest.raises(ValueError, match="attempt URL"):
        assemble(tmp_path / "i", evidence_fragments(tmp_path / "url"), "https://github.com/example/repo/actions/runs/1", PASSED)
    # Symlinked fragment directories and control files are refused before anything is read.
    linked = evidence_fragments(tmp_path / "link")
    (linked / "ci-evidence-load" / "source-sha").unlink()
    (linked / "ci-evidence-load" / "source-sha").symlink_to(linked / "ci-evidence-verify" / "source-sha")
    with pytest.raises(ValueError, match="does not produce"):
        assemble(tmp_path / "j", linked, ATTEMPT_URL, PASSED)
    elsewhere = evidence_fragments(tmp_path / "dir")
    moved = tmp_path / "outside-load"
    (elsewhere / "ci-evidence-load").rename(moved)
    (elsewhere / "ci-evidence-load").symlink_to(moved, target_is_directory=True)
    with pytest.raises(ValueError, match="plain directory"):
        assemble(tmp_path / "k", elsewhere, ATTEMPT_URL, PASSED)


def test_missing_load_fragment_is_recorded_as_an_unrun_check(tmp_path):
    from scripts.tiden_ci import assemble
    merged = assemble(tmp_path / "ci-evidence", evidence_fragments(tmp_path, load=False), ATTEMPT_URL, PASSED)
    assert merged["fragments"] == ["verify", *SHARD_JOBS, "go"] and merged["missing"] == ["load"] and merged["module_checks"]["load"] == "skipped"
    assert assembled_rows(tmp_path, merged)["deedkit-ci::load"] == "blocked"


def test_failed_rerun_never_publishes_an_earlier_attempts_passing_fragment(tmp_path):
    from scripts.tiden_ci import assemble
    # The load job passed in attempt 1, then failed in attempt 2 before uploading: its attempt-1 fragment remains.
    out = tmp_path / "ci-evidence"
    merged = assemble(out, evidence_fragments(tmp_path, load_attempt="1"), ATTEMPT_URL,
                      {**PASSED, "load": ("failure", "a" * 40)})
    assert merged["missing"] == ["load"] and merged["module_checks"]["load"] == "failure"
    assert not (out / "load.xml").exists()
    assert assembled_rows(tmp_path, merged)["deedkit-ci::load"] == "blocked"  # failed without a report: never passed
    # A failed verify without a fresh fragment turns all seven checks red, not passed.
    stale_verify = evidence_fragments(tmp_path / "v")
    (stale_verify / "ci-evidence-verify" / "attempt").write_text("1\n")
    merged = assemble(tmp_path / "v" / "ci-evidence", stale_verify, ATTEMPT_URL, {**PASSED, "verify": ("cancelled", "")})
    assert set(merged["checks"].values()) == {"cancelled"}
    # Its stale shard-1 report is never merged; the python report holds only the fresh shards' cases.
    merged_report = ET.parse(tmp_path / "v" / "ci-evidence" / "python.xml").getroot()
    assert [case.get("name") for case in merged_report.iter("testcase")] == list(SHARD_JOBS)
    # A stale verify fragment never hides that the failed verify job captured another checkout.
    with pytest.raises(ValueError, match="captured another source"):
        assemble(tmp_path / "v" / "other", stale_verify, ATTEMPT_URL, {**PASSED, "verify": ("failure", "b" * 40)})
    # A fragment must name the attempt it was uploaded in.
    unnamed = evidence_fragments(tmp_path / "unnamed")
    (unnamed / "ci-evidence-load" / "attempt").unlink()
    with pytest.raises(ValueError, match="attempt"):
        assemble(tmp_path / "unnamed" / "ci-evidence", unnamed, ATTEMPT_URL, PASSED)


def test_evidence_step_records_and_then_fails_a_run_without_every_fragment(tmp_path):
    import subprocess
    import yaml
    root = Path(__file__).resolve().parents[1]
    evidence = yaml.safe_load((root / ".github/workflows/checks.yml").read_text())["jobs"]["evidence"]
    step = next(s for s in evidence["steps"] if s.get("name") == "Merge the fragments of the tested commit and record every check outcome")
    assert step["env"] == {"VERIFY": "verify:${{ needs.verify.result }}:${{ needs.verify.outputs.source_sha }}",
                           "LOAD": "load:${{ needs.load.result }}:${{ needs.load.outputs.source_sha }}",
                           **{f"PYTHON_{index}": f"python-{index}:${{{{ needs.python-{index}.result }}}}:${{{{ needs.python-{index}.outputs.source_sha }}}}"
                              for index in range(2, PYTHON_SHARDS + 1)},
                           "GO": "go:${{ needs.go.result }}:${{ needs.go.outputs.source_sha }}"}
    work = tmp_path / "work"
    work.mkdir()
    (work / "ci-tools").symlink_to(root, target_is_directory=True)
    temp = tmp_path / "runner"
    temp.mkdir()
    evidence_fragments(temp, load=False)  # both jobs report success, the load artifact is gone, Tiden is unconfigured
    env = {"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin", "RUNNER_TEMP": str(temp), "GITHUB_REPOSITORY": "example/repo",
           "GITHUB_RUN_ID": "1", "GITHUB_RUN_ATTEMPT": "2", "VERIFY": "verify:success:" + "a" * 40,
           "LOAD": "load:success:" + "a" * 40, **{f"PYTHON_{index}": f"python-{index}:success:" + "a" * 40 for index in range(2, PYTHON_SHARDS + 1)},
           "GO": "go:success:" + "a" * 40}
    result = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=work, capture_output=True, text=True, env=env)
    assert result.returncode != 0, result.stderr
    rows = json.loads((temp / "ci-evidence" / "ci-checks.json").read_text())  # the evidence still exists for upload
    assert next(r for r in rows if r["signature"] == "deedkit-ci::load")["execution"]["status"] == "blocked"
    load = temp / "fragments" / "ci-evidence-load"
    load.mkdir()
    for name, value in {"source-sha": "a" * 40, "attempt": "2", "load.xml": LOAD_REPORT, "load.json": "{}",
                        "outcomes.json": json.dumps({"module_checks": {"load": "success"}})}.items():
        (load / name).write_text(value)
    for path in (temp / "ci-evidence").iterdir():
        path.unlink()
    assert subprocess.run(["bash", "-e", "-c", step["run"]], cwd=work, capture_output=True, env=env).returncode == 0


def test_load_gate_runs_in_its_own_parallel_job_and_every_test_job_gates_the_evidence():
    import subprocess
    import yaml
    root = Path(__file__).resolve().parents[1]
    jobs = yaml.safe_load((root / ".github/workflows/checks.yml").read_text())["jobs"]
    load = jobs["load"]
    # Its own runner, in parallel with verify, bounded, with the unchanged command and budgets.
    # Skipped, like verify, only on a main push that reuses the merge queue's verification (ANYM-322), on a merge
    # queue run that reuses a full pull request run of its tree (ANYM-358) or on a docs-only pull request run
    # (ANYM-323); it needs only that decision, never verify.
    assert load["needs"] == jobs["verify"]["needs"] == "documentation"
    assert load["if"] == jobs["verify"]["if"] == "${{ !cancelled() && !inputs.reuse_queue_evidence && !inputs.reuse_pr_verification && needs.documentation.outputs.docs_only != 'true' }}"
    assert load["timeout-minutes"] == 10
    gate = next(step for step in load["steps"] if step.get("id") == "load")
    assert gate["name"] == "Paired gateway load and isolation budgets" and "if" not in gate
    assert "gateway_read_load.py --config tests/platform/benchmarks/budgets.json" in gate["run"]
    assert all(step.get("id") != "load" for step in jobs["verify"]["steps"])
    assert set(CHECKS) <= {step.get("id") for step in jobs["verify"]["steps"]}
    uploads = {job: next(step for step in jobs[job]["steps"] if step.get("uses", "").startswith("actions/upload-artifact")
                         and step["with"]["name"].startswith("ci-evidence")) for job in ("verify", "load")}
    for job, upload in uploads.items():
        assert upload["with"]["name"] == f"ci-evidence-{job}" and upload["if"] == "${{ always() }}"
        assert upload["with"]["overwrite"] is True
    evidence = jobs["evidence"]
    assert evidence["needs"] == ["documentation", "verify", "load", *SHARD_JOBS, "go"] and evidence["if"] == "${{ always() && !inputs.reuse_queue_evidence && !inputs.reuse_pr_verification && needs.documentation.outputs.docs_only != 'true' }}"
    assert jobs["tiden"]["needs"] == ["documentation", "evidence"]
    steps = {step.get("name") or step["uses"].split("@")[0]: step for step in evidence["steps"]}
    assert steps["actions/download-artifact"]["with"]["pattern"] == "ci-evidence-*"
    assert "tiden_ci.py assemble" in steps["Merge the fragments of the tested commit and record every check outcome"]["run"]
    retained = steps["Retain test evidence even when a check fails"]["with"]
    assert retained["name"] == "ci-evidence" and retained["overwrite"] is True  # a rerun attempt replaces it
    require = steps["Require every test job to have succeeded"]
    assert require["if"] == "${{ always() }}"
    assert require["env"] == {"VERIFY_RESULT": "${{ needs.verify.result }}", "LOAD_RESULT": "${{ needs.load.result }}",
                              **{f"PYTHON_{index}_RESULT": f"${{{{ needs.python-{index}.result }}}}" for index in range(2, PYTHON_SHARDS + 1)},
                              "GO_RESULT": "${{ needs.go.result }}"}
    for verify, load_result, shards, go in [("success", "success", "success", "success"), ("success", "failure", "success", "success"),
                                            ("success", "cancelled", "success", "success"), ("success", "skipped", "success", "success"),
                                            ("failure", "success", "success", "success"), ("skipped", "success", "success", "success"),
                                            ("success", "success", "failure", "success"), ("success", "success", "skipped", "success"),
                                            ("success", "success", "success", "failure"), ("success", "success", "success", "skipped")]:
        result = subprocess.run(["bash", "-e", "-c", require["run"]], capture_output=True,
                                env={"VERIFY_RESULT": verify, "LOAD_RESULT": load_result, "GO_RESULT": go,
                                     **{f"PYTHON_{index}_RESULT": "success" for index in range(2, PYTHON_SHARDS + 1)},
                                     "PYTHON_3_RESULT": shards})
        assert (result.returncode == 0) == (verify == load_result == shards == go == "success"), (verify, load_result, shards, go)


def test_python_suite_reports_its_slowest_tests_and_waits_for_its_background_images(tmp_path):
    import subprocess
    import yaml
    root = Path(__file__).resolve().parents[1]
    steps = yaml.safe_load((root / ".github/workflows/checks.yml").read_text())["jobs"]["verify"]["steps"]
    names = [step.get("name", "") for step in steps]
    start = names.index("Start the release router image build and the Python image pull in the background")
    wait = names.index("Wait for the background router image and Python image")
    python = next(i for i, step in enumerate(steps) if step.get("id") == "python")
    # The image preparation starts before the module test steps and is awaited right before the suite.
    assert start < names.index("Gateway isolated package and transport tests") and wait == python - 1
    assert "docker build --file services/release-router/Dockerfile" in steps[start]["run"]
    assert "docker build" not in steps[python]["run"] and "docker pull" not in steps[python]["run"]
    assert "--durations=25" in steps[python]["run"]
    background = tmp_path / "background"

    def waits(**exits):
        background.mkdir(exist_ok=True)
        for path in background.iterdir():
            path.unlink()
        for name, code in exits.items():
            (background / f"{name.replace('_', '-')}.exit").write_text(code + "\n")
        return subprocess.run(["bash", "-e", "-c", steps[wait]["run"]], capture_output=True,
                              env={"PATH": "/usr/bin:/bin", "RUNNER_TEMP": str(tmp_path), "BACKGROUND_WAIT_SECONDS": "1"})

    assert waits(router="0", python_image="0").returncode == 0
    assert waits(router="1", python_image="0").returncode != 0
    assert waits(router="0", python_image="1").returncode != 0
    assert waits(router="0").returncode != 0  # a preparation that never finished fails too
    # The real start step returns before its background jobs finish, and the wait step then sees their results.
    import time
    for path in background.iterdir():
        path.unlink()
    tools = tmp_path / "bin"
    tools.mkdir()
    (tools / "docker").write_text('#!/bin/sh\nsleep 5\ntest "$1" = build\n')  # the pull fails, the build succeeds
    (tools / "docker").chmod(0o755)
    env = {"PATH": f"{tools}:/usr/bin:/bin", "RUNNER_TEMP": str(tmp_path), "DEEDKIT_TEST_PYTHON_IMAGE": "synthetic"}
    began = time.monotonic()
    started = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", steps[start]["run"]], cwd=root, capture_output=True,
                             env=env, timeout=10)
    assert started.returncode == 0 and time.monotonic() - began < 4
    waited = subprocess.run(["bash", "-e", "-c", steps[wait]["run"]], capture_output=True, timeout=30,
                            env={**env, "BACKGROUND_WAIT_SECONDS": "20"})
    assert waited.returncode != 0 and b"python-image" in waited.stdout
    assert (background / "router.exit").read_text().strip() == "0"


PASSING = '<testsuite><testcase name="x" file="tests/x.py"/></testsuite>'


def evidence_verdict(tmp_path, meta, **reports):
    """The evidence job's verdict step, run on evidence that the real prepare wrote from synthetic reports."""
    import os
    import subprocess
    import yaml
    root = Path(__file__).resolve().parents[1]
    steps = yaml.safe_load((root / ".github/workflows/checks.yml").read_text())["jobs"]["evidence"]["steps"]
    step = next(step for step in steps if step.get("name") == "Require every recorded test and check to have passed")
    directory = tmp_path / "ci-evidence"
    directory.mkdir()
    for name, xml in reports.items():
        (directory / f"{name}.xml").write_text(xml)
    prepare(tmp_path, directory, meta)
    (tmp_path / "ci-tools").symlink_to(root)  # the workflow commit's checkout in CI
    return subprocess.run(["bash", "-e", "-c", step["run"]], cwd=tmp_path, capture_output=True, text=True,
                          env={"PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}",
                               "RUNNER_TEMP": str(tmp_path)})


@pytest.mark.parametrize("change, reports, passes", [
    ({}, {"python": PASSING, "collector": PASSING, "npm": PASSING}, True),
    ({}, {"python": '<testsuite><testcase name="x" file="tests/x.py"><failure/></testcase></testsuite>',
          "collector": PASSING, "npm": PASSING}, False),
    ({}, {"python": PASSING, "collector": PASSING}, False),
    ({}, {"python": '<testsuite><testcase name="x" file="tests/x.py"><skipped/></testcase></testsuite>',
          "collector": PASSING, "npm": PASSING}, False),
    ({"smoke": "failure"}, {"python": PASSING, "collector": PASSING, "npm": PASSING}, False),
    ({"distributions": "skipped"}, {"python": PASSING, "collector": PASSING, "npm": PASSING}, False),
], ids=["passed", "failed-test", "missing-report", "only-skipped", "failed-step", "skipped-step"])
def test_the_evidence_job_fails_on_a_failed_test_or_a_failed_or_blocked_check(tmp_path, change, reports, passes):
    # The gate takes this verdict from the evidence job, never from the Tiden publication.
    meta = metadata()
    meta["checks"].update(change)
    completed = evidence_verdict(tmp_path, meta, **reports)
    assert completed.returncode == (0 if passes else 1), completed.stdout + completed.stderr
    assert completed.stdout.startswith("Every test and check passed." if passes else "The evidence records ")
    import yaml
    root = Path(__file__).resolve().parents[1]
    jobs = yaml.safe_load((root / ".github/workflows/checks.yml").read_text())["jobs"]
    names = [step.get("name") for step in jobs["evidence"]["steps"]]
    verdict = names.index("Require every recorded test and check to have passed")
    # Evaluated after the evidence is retained, also when an earlier step failed, and its failure fails the job.
    assert names.index("Retain test evidence even when a check fails") < verdict
    assert jobs["evidence"]["steps"][verdict]["if"] == "${{ always() }}"
    assert "continue-on-error" not in jobs["evidence"]["steps"][verdict]
    assert "continue-on-error" not in jobs["evidence"] and "continue-on-error" not in jobs["tiden"]


def test_a_failed_tiden_publication_warns_and_never_fails_the_reporting_job(tmp_path):
    import os
    import subprocess
    import yaml
    root = Path(__file__).resolve().parents[1]
    ci = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())
    job = yaml.safe_load((root / ".github/workflows/checks.yml").read_text())["jobs"]["tiden"]
    steps = {step.get("name") or step["uses"].split("@")[0]: step for step in job["steps"]}
    install, publication = "Install pinned Tiden CLI with integrity check", "Publish and verify branch association"
    warn = "Warn that the evidence was not published to Tiden"
    # Only the CLI install and the publication may fail without failing the job. Queue, docs-only and reuse
    # receipts still fail it, and so does the job itself: no job-level failure suppression.
    assert [name for name, step in steps.items() if step.get("continue-on-error")] == [install, publication]
    assert "continue-on-error" not in job
    assert (steps[install]["id"], steps[publication]["id"]) == ("install", "publish")
    # Every step has its own timeout and all of them together leave the job two minutes for setup and cleanup, so
    # slow but successful prerequisites never let the job deadline cancel a hung Tiden call before its step's.
    assert all("timeout-minutes" in step for step in steps.values())
    assert sum(step["timeout-minutes"] for step in steps.values()) + 2 <= job["timeout-minutes"]
    # The release record learns from this output whether the publication was verified.
    assert job["outputs"]["published"] == ("${{ steps.publish.outcome == 'success' "
                                           "&& steps.publish.outputs.verified == 'true' }}")
    assert list(steps).index(warn) == list(steps).index(publication) + 1
    assert steps[warn]["if"] == ("${{ always() && steps.config.outputs.enabled == 'true' && "
                                 "(steps.install.outcome != 'success' || steps.publish.outcome != 'success') }}")
    retain = steps["Retain Tiden association and verdict receipt"]
    assert retain["if"].startswith("${{ always() && ((steps.config.outputs.enabled == 'true'")
    assert retain["with"]["path"] == "${{ runner.temp }}/ci-evidence/tiden-*.json"
    # The gate sees the reporting job only through the checks result, which a step allowed to fail leaves successful.
    assert ci["jobs"]["gate"]["needs"] == ["reuse", "pr_reuse", "checks", "native", "images"]
    assert 'test "$CHECKS_RESULT" = success' in ci["jobs"]["gate"]["steps"][0]["run"]

    def warned(install_outcome, publish_outcome):
        temp = tmp_path / f"{install_outcome}-{publish_outcome}"
        temp.mkdir()
        summary = temp / "summary"
        completed = subprocess.run(["bash", "-e", "-c", steps[warn]["run"]], capture_output=True, text=True, env={
            "PATH": os.environ["PATH"], "RUNNER_TEMP": str(temp), "GITHUB_STEP_SUMMARY": str(summary),
            "INSTALL": install_outcome, "PUBLISH": publish_outcome})
        receipt = json.loads((temp / "ci-evidence" / "tiden-publication.json").read_text())
        return completed, receipt, summary.read_text()

    for install_outcome, publish_outcome in (("success", "failure"), ("failure", "failure")):
        completed, receipt, summary = warned(install_outcome, publish_outcome)
        assert completed.returncode == 0
        assert completed.stdout.startswith("::warning title=Tiden evidence not published::")
        assert f"CLI install: {install_outcome}; publication: {publish_outcome}" in completed.stdout
        assert receipt == {"published": False, "install": install_outcome, "publish": publish_outcome}
        assert summary.startswith("Tiden evidence not published.")


def test_a_main_commit_whose_publication_was_not_verified_records_no_release(tmp_path):
    import os
    import subprocess
    import yaml
    root = Path(__file__).resolve().parents[1]
    ci = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())
    checks = yaml.safe_load((root / ".github/workflows/checks.yml").read_text())
    output = checks.get("on", checks.get(True))["workflow_call"]["outputs"]["tiden_published"]
    assert output["value"] == "${{ jobs.tiden.outputs.published }}"
    release = ci["jobs"]["release"]
    assert "checks" in release["needs"]
    config = release["steps"][0]
    assert config["id"] == "config" and config["env"]["TIDEN_PUBLISHED"] == "${{ needs.checks.outputs.tiden_published }}"
    # Every later step, Tiden calls included, runs only when the configuration step enabled the record.
    assert all(step["if"] in ("steps.config.outputs.enabled == 'true'",
                              "steps.config.outputs.enabled == 'true' && steps.download.outcome == 'success'",
                              "steps.config.outputs.enabled == 'true' && steps.download.outcome != 'success'",
                              "${{ always() && steps.config.outputs.enabled == 'true' }}") for step in release["steps"][1:])

    def configured(published, token="synthetic-token"):
        temp = tmp_path / f"{published or 'unset'}-{bool(token)}"
        temp.mkdir()
        completed = subprocess.run(["bash", "-e", "-c", config["run"]], capture_output=True, text=True, env={
            "PATH": os.environ["PATH"], "GITHUB_OUTPUT": str(temp / "output"), "GITHUB_STEP_SUMMARY": str(temp / "summary"),
            "TIDEN_API_TOKEN": token, "TIDEN_PRODUCT_ID": "product", "TIDEN_WORKSPACE_ID": "workspace",
            "TIDEN_PUBLISHED": published})
        return completed, (temp / "output").read_text()

    completed, written = configured("true")
    assert completed.returncode == 0 and written == "enabled=true\n" and not completed.stdout
    for published in ("false", ""):
        completed, written = configured(published)
        assert completed.returncode == 0 and written == "enabled=false\n"
        assert completed.stdout.startswith("::warning title=Tiden release not recorded::")
    completed, written = configured("false", token="")
    assert completed.returncode == 0 and written == "enabled=false\n" and not completed.stdout


# spec: ANYM-421
def test_the_go_job_uploads_both_native_go_suites_as_a_fragment_the_evidence_job_requires(tmp_path):
    import os
    import subprocess
    import yaml
    from scripts.tiden_ci import FRAGMENTS
    root = Path(__file__).resolve().parents[1]
    jobs = yaml.safe_load((root / ".github/workflows/checks.yml").read_text())["jobs"]
    go = jobs["go"]
    # Skipped exactly when the other test jobs are: a reusing push or queue run, or a docs-only run.
    assert (go["needs"], go["if"]) == (jobs["load"]["needs"], jobs["load"]["if"])
    steps = {step.get("id") or step.get("name"): step for step in go["steps"]}
    for check, directory, report in (("whatsmeow", "whatsapp/whatsmeow", "whatsmeow.json"),
                                     ("release_router", "services/release-router", "release-router.json")):
        assert steps[check]["working-directory"] == directory
        assert steps[check]["run"] == f'go test -race -count=1 -json ./... > "$RUNNER_TEMP/ci-evidence/{report}"'
    assert steps["release_router"]["if"] == "${{ !cancelled() }}"  # runs, into its own file, after a whatsmeow failure
    record = steps["Record the Go outcomes"]
    assert record["if"] == "${{ always() }}" and record["env"] == {
        "WHATSMEOW_OUTCOME": "${{ steps.whatsmeow.outcome }}", "RELEASE_ROUTER_OUTCOME": "${{ steps.release_router.outcome }}"}
    (tmp_path / "ci-evidence").mkdir()
    recorded = subprocess.run(["bash", "-e", "-c", record["run"]], capture_output=True, text=True, env={
        "PATH": os.environ["PATH"], "RUNNER_TEMP": str(tmp_path), "WHATSMEOW_OUTCOME": "failure", "RELEASE_ROUTER_OUTCOME": "success"})
    assert recorded.returncode == 0, recorded.stderr
    assert json.loads((tmp_path / "ci-evidence" / "outcomes.json").read_text()) == {
        "module_checks": {"whatsmeow": "failure", "release_router": "success"}}
    assert FRAGMENTS["go"] == (("whatsmeow", "release_router"), ("outcomes.json", "whatsmeow.json", "release-router.json"))
    upload = next(step for step in go["steps"] if step.get("uses", "").startswith("actions/upload-artifact"))
    assert upload["if"] == "${{ always() }}" and upload["with"]["name"] == "ci-evidence-go"
    assert upload["with"]["path"] == "${{ runner.temp }}/ci-evidence" and upload["with"]["overwrite"] is True
    evidence = jobs["evidence"]
    assert "go" in evidence["needs"]
    evidence_steps = {step.get("name"): step for step in evidence["steps"]}
    merge = evidence_steps["Merge the fragments of the tested commit and record every check outcome"]
    assert merge["env"]["GO"] == "go:${{ needs.go.result }}:${{ needs.go.outputs.source_sha }}"
    assert '--producer "$GO"' in merge["run"]
    require = evidence_steps["Require every test job to have succeeded"]
    assert require["env"]["GO_RESULT"] == "${{ needs.go.result }}" and 'test "$GO_RESULT" = success' in require["run"].splitlines()
    # The Tiden configuration check tests each ID on its own line, so under bash -e either missing ID fails the step.
    config = next(step for step in jobs["tiden"]["steps"] if step.get("id") == "config")
    lines = [line.strip() for line in config["run"].splitlines()]
    assert 'test -n "$TIDEN_PRODUCT_ID"' in lines and 'test -n "$TIDEN_WORKSPACE_ID"' in lines
    assert not any("test -n" in line and "&&" in line for line in lines)

    def configured(product, workspace):
        output = tmp_path / f"output-{bool(product)}-{bool(workspace)}"
        output.write_text("")
        completed = subprocess.run(["bash", "-e", "-c", config["run"]], capture_output=True, text=True, env={
            "PATH": os.environ["PATH"], "GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
            "TIDEN_API_TOKEN": "synthetic-token", "TIDEN_PRODUCT_ID": product, "TIDEN_WORKSPACE_ID": workspace,
            "EVENT_NAME": "push", "SOURCE_BRANCH": "main", "DOCS_ONLY": "false", "PR_REUSED": "false"})
        return completed.returncode, output.read_text()
    assert configured("product", "workspace") == (0, "enabled=true\n")
    for product, workspace in (("", "workspace"), ("product", "")):
        code, written = configured(product, workspace)
        assert code != 0 and "enabled=true" not in written, (product, workspace)


GO_SOURCE = """func TestPasses(t *testing.T) {}
func TestFails(t *testing.T) {}
func TestSkipped(t *testing.T) {}
func TestParent(t *testing.T) {}
func TestHangs(t *testing.T) {}
"""


# spec: ANYM-421
def test_go_subtest_names_with_slashes_colons_or_spaces_keep_their_exact_leaf_identity(tmp_path):
    from scripts.tiden_ci import go_results
    module = go_module(tmp_path, "whatsmeow", "func TestSlashes(t *testing.T) {}\nfunc TestSymbols(t *testing.T) {}\n")
    events = []
    # `t.Run("a/b", ...)` reports `TestSlashes/a/b` with no `TestSlashes/a` event; `go test` writes a space as `_`.
    for name in ("TestSlashes", "TestSlashes/a/b", "TestSymbols", "TestSymbols/with:colon", "TestSymbols/with_space",
                 "TestSymbols/ünïcode"):
        events.append({"Action": "run", "Package": module, "Test": name})
    for name in ("TestSlashes/a/b", "TestSlashes", "TestSymbols/with:colon", "TestSymbols/with_space", "TestSymbols/ünïcode",
                 "TestSymbols"):
        events.append({"Action": "pass", "Package": module, "Test": name, "Elapsed": 0.01})
    stream = tmp_path / "whatsmeow.json"
    stream.write_text(go_stream(*events, {"Action": "pass", "Package": module}))
    rows, counts, unexplained = go_results(stream, tmp_path, "whatsapp/whatsmeow", ATTEMPT_URL, "whatsmeow")
    assert [row["signature"] for row in rows] == [f"{module}::TestSlashes/a/b", f"{module}::TestSymbols/with:colon",
                                                  f"{module}::TestSymbols/with_space", f"{module}::TestSymbols/ünïcode"]
    assert counts == Counter(passed=4) and not unexplained


# spec: ANYM-421
def test_go_metadata_events_such_as_attr_and_artifacts_leave_the_test_passed_and_add_nothing(tmp_path):
    from scripts.tiden_ci import go_results
    module = go_module(tmp_path, "whatsmeow")
    # Emitted by Go 1.26 for t.Attr and, with -artifacts, t.ArtifactDir.
    stream = tmp_path / "whatsmeow.json"
    stream.write_text(go_stream({"Action": "run", "Package": module, "Test": "TestPasses"},
                                {"Action": "attr", "Package": module, "Test": "TestPasses", "Key": "k", "Value": "SECRET-ATTR"},
                                {"Action": "artifacts", "Package": module, "Test": "TestPasses", "Path": "/tmp/SECRET-DIR"},
                                {"Action": "pass", "Package": module, "Test": "TestPasses", "Elapsed": 0.01},
                                {"Action": "pass", "Package": module}))
    rows, counts, unexplained = go_results(stream, tmp_path, "whatsapp/whatsmeow", ATTEMPT_URL, "whatsmeow")
    assert [(row["signature"], row["execution"]["status"]) for row in rows] == [(f"{module}::TestPasses", "passed")]
    assert counts == Counter(passed=1) and not unexplained and "SECRET" not in json.dumps(rows)


# spec: ANYM-421
@pytest.mark.parametrize("text, module", [
    ("module example.com/bare\n", "example.com/bare"), ('module "example.com/quoted"\n', "example.com/quoted"),
    ("module `example.com/raw`\n", "example.com/raw"), ("module example.com/commented // note\n", "example.com/commented"),
    ("// module example.com/not\ngo 1.26\n", None), ("go 1.26\n", None),
    ("module example.com/tight//note\n", "example.com/tight"), ('module "example.com/\\x73caped"\n', "example.com/scaped"),
    ("module (\n\texample.com/block\n)\n", None)],
    ids=["bare", "quoted", "raw", "commented", "commented-out", "none", "comment-without-space", "escaped-quoted",
         "block-form"])
def test_a_go_mod_module_path_is_read_bare_quoted_or_commented(tmp_path, text, module):
    from scripts.tiden_ci import go_module_path, go_results
    assert go_module_path(text) == module
    if module:
        directory = tmp_path / "whatsapp/whatsmeow"
        directory.mkdir(parents=True)
        (directory / "go.mod").write_text(text)
        (directory / "x_test.go").write_text('package x\n\nimport "testing"\n\nfunc TestPasses(t *testing.T) {}\n')
        stream = tmp_path / "whatsmeow.json"
        stream.write_text(passing_go_stream(module))
        rows = go_results(stream, tmp_path, "whatsapp/whatsmeow", ATTEMPT_URL, "whatsmeow")[0]
        assert [row["signature"] for row in rows] == [f"{module}::TestPasses"]


# spec: ANYM-421
def test_a_go_test_stream_becomes_one_result_row_per_test_under_its_existing_go_identity(tmp_path):
    from scripts.tiden_ci import go_results, row_id
    module = go_module(tmp_path, "whatsmeow", GO_SOURCE, file="cases_test.go")
    deep = tmp_path / "whatsapp/whatsmeow/inner/deep"
    deep.mkdir(parents=True)
    (deep / "deep_test.go").write_text('package deep\n\nimport "testing"\n\nfunc TestDeep(t *testing.T) {}\n')
    secret = "SYNTHETIC-OUTPUT-NEVER-PUBLISHED"

    def test(action, name, package=module, **extra):
        return {"Action": action, "Package": package, "Test": name, **extra}
    events = [{"Action": "start", "Package": module}]
    for name in ("TestPasses", "TestFails", "TestSkipped", "TestParent", "TestParent/first", "TestParent/second", "TestHangs"):
        events += [test("run", name), test("output", name, Output=f"=== RUN {name} {secret}\n")]
    events += [test("pass", "TestPasses", Elapsed=0.25), test("fail", "TestFails", Elapsed=0.5), test("skip", "TestSkipped"),
               test("pass", "TestParent/first", Elapsed=0.1), test("fail", "TestParent/second", Elapsed=0.2),
               test("fail", "TestParent", Elapsed=0.3), {"Action": "output", "Package": module, "Output": f"FAIL {secret}\n"},
               {"Action": "fail", "Package": module, "Elapsed": 1}]
    package = module + "/inner/deep"
    events += [test("run", "TestDeep", package), test("pass", "TestDeep", package, Elapsed=0.01), {"Action": "pass", "Package": package}]
    stream = tmp_path / "whatsmeow.json"
    stream.write_text(go_stream(*events))
    url = "https://github.com/example/repo/actions/runs/1/attempts/2"
    rows, counts, unexplained = go_results(stream, tmp_path, "whatsapp/whatsmeow", url, "whatsmeow")

    def row(package, name, status, duration, suite, file):
        signature = f"{package}::{name}"
        return {"id": row_id(url, "whatsmeow", signature), "title": name, "signature": signature,
                "execution": {"status": status, "duration": duration}, "suitePath": [{"title": suite}],
                "fields": {"file_path": file}}
    expected = [row(module, "TestPasses", "passed", 250, "whatsmeow", "cases_test.go"),
                row(module, "TestFails", "failed", 500, "whatsmeow", "cases_test.go"),
                row(module, "TestSkipped", "skipped", 0, "whatsmeow", "cases_test.go"),
                row(module, "TestParent/first", "passed", 100, "whatsmeow", "cases_test.go"),
                row(module, "TestParent/second", "failed", 200, "whatsmeow", "cases_test.go"),
                row(module, "TestHangs", "failed", 0, "whatsmeow", "cases_test.go"),  # started, never finished
                row(package, "TestDeep", "passed", 10, "deep", "deep_test.go")]
    # A test with subtests is no row of its own: its subtests are.
    assert sorted(rows, key=lambda r: r["signature"]) == sorted(expected, key=lambda r: r["signature"])
    assert counts == Counter(passed=3, failed=3, skipped=1)
    assert unexplained is False  # the package failure is explained by its failed tests
    assert secret not in json.dumps(rows) and "Output" not in json.dumps(rows)


# spec: ANYM-421
def test_the_real_go_suites_produce_every_existing_tiden_go_identity(tmp_path):
    import os
    import shutil
    import subprocess
    from scripts.tiden_ci import MODULE_REPORTS, go_results
    if shutil.which("go") is None:
        if os.environ.get("CI"):
            pytest.fail("go is required on CI to check the Go identities")
        pytest.skip("go is not installed")
    root = Path(__file__).resolve().parents[1]
    rows = []
    for check in ("whatsmeow", "release_router"):
        stream = tmp_path / f"{check}.json"
        with stream.open("wb") as output:
            subprocess.run(["go", "test", "-count=1", "-json", "./..."], cwd=root / MODULE_REPORTS[check][2], stdout=output,
                           stderr=subprocess.DEVNULL, timeout=300, check=False)
        found, _, unexplained = go_results(stream, root, MODULE_REPORTS[check][2], ATTEMPT_URL, check)
        assert not unexplained, check
        rows += found
    produced = {row["signature"]: (row["title"], row["suitePath"], row["fields"]["file_path"]) for row in rows}
    identities = json.loads((root / "tests/fixtures/tiden-go-identities.json").read_text())
    assert identities and {item["signature"]: (item["title"], [{"title": item["suite"]}], item["file"])
                           for item in identities}.items() <= produced.items()
    assert {row["execution"]["status"] for row in rows} == {"passed"}
    # Leaf-only: no row is an ancestor of another, so a new test cannot add a parent row unnoticed.
    assert not [name for name in produced if any(other.startswith(name + "/") for other in produced)]


def broken_go_stream(case, root, module):
    """A `go test -json` stream of `module` (under `root`) that must never yield a passed check."""
    passing = {"Action": "pass", "Package": module, "Test": "TestPasses", "Elapsed": 0.1}
    if case == "no-test-result":
        return go_stream({"Action": "start", "Package": module}, {"Action": "output", "Package": module, "Output": "ok\n"},
                         {"Action": "pass", "Package": module})
    if case == "non-json-line":
        return go_stream(passing, "panic: not an event")
    if case == "non-object-event":
        return go_stream(passing, "[1, 2]")
    if case == "package-outside-module":
        # Only a name prefix of the module: read as the module's own directory, it would name TestPasses' file.
        return go_stream(passing, {"Action": "pass", "Package": module + "x", "Test": "TestPasses"})
    if case == "package-escaping-module":
        # `..` in the package path would read another directory's test files as this module's.
        (root / "whatsapp/escaped").mkdir(parents=True, exist_ok=True)
        (root / "whatsapp/escaped/e_test.go").write_text('package e\n\nimport "testing"\n\nfunc TestEscaped(t *testing.T) {}\n')
        return go_stream(passing, {"Action": "pass", "Package": module + "/../escaped", "Test": "TestEscaped"})
    if case == "unhashable-action":
        return go_stream(passing, {"Action": [], "Package": module, "Test": "TestPasses"})
    if case == "empty-event":
        return go_stream(passing, {})
    if case == "foreign-package-event":
        return go_stream(passing, {"Action": "pass", "Package": "example.com/foreign"})
    if case == "undeclared-test":
        return go_stream(passing, {"Action": "pass", "Package": module, "Test": "TestNowhere"})
    if case == "ambiguous-test-file":
        (root / "whatsapp/whatsmeow/twice_test.go").write_text('package x\n\nimport "testing"\n\nfunc TestPasses(t *testing.T) {}\n')
        return go_stream(passing)
    if case == "unexplained-package-failure":  # a panic after the tests, or a package that failed to build
        return go_stream(passing, {"Action": "fail", "Package": module, "Elapsed": 0.2},
                         {"Action": "start", "Package": module + "/broken"},
                         {"Action": "fail", "Package": module + "/broken", "FailedBuild": module + "/broken [build failed]"})
    raise AssertionError(case)


# spec: ANYM-421
@pytest.mark.parametrize("case, status", [
    ("no-test-result", "blocked"), ("non-json-line", "blocked"), ("non-object-event", "blocked"),
    ("package-outside-module", "blocked"), ("package-escaping-module", "blocked"), ("unhashable-action", "blocked"),
    ("empty-event", "blocked"), ("foreign-package-event", "blocked"), ("undeclared-test", "blocked"), ("ambiguous-test-file", "blocked"),
    ("unexplained-package-failure", "failed")])
def test_an_unusable_go_stream_fails_only_its_own_check(tmp_path, case, status):
    from scripts.tiden_ci import MODULE_REPORTS
    root, directory = tmp_path / "source", tmp_path / "ci-evidence"
    directory.mkdir()
    (directory / "whatsmeow.json").write_text(broken_go_stream(case, root, go_module(root, "whatsmeow")))
    (directory / "release-router.json").write_text(passing_go_stream(go_module(root, "release_router")))
    (directory / "python.xml").write_text(PASSING)
    meta = {**metadata(), "module_checks": dict.fromkeys(MODULE_REPORTS, "success")}
    manifest = prepare(root, directory, meta)
    rows = {row["signature"]: row for row in json.loads((directory / "ci-checks.json").read_text())}
    assert rows["deedkit-ci::whatsmeow"]["execution"]["status"] == status
    if status == "blocked":  # a ValueError: no report of it is published, and the check says why
        assert rows["deedkit-ci::whatsmeow"]["message"] == "Missing or invalid test report; verification incomplete"
        assert "normalized-whatsmeow.json" not in manifest["reports"] and not (directory / "normalized-whatsmeow.json").exists()
    # The other Go module and the other reports are prepared all the same.
    assert rows["deedkit-ci::release_router"]["execution"]["status"] == rows["anymsg-ci::python"]["execution"]["status"] == "passed"
    assert {"normalized-release_router.json", "normalized-python.xml"} <= set(manifest["reports"])
    assert json.loads((directory / "normalized-release_router.json").read_text())[0]["signature"] == \
        GO_MODULE_PATHS["release_router"] + "::TestPasses"


# spec: ANYM-421
def test_a_missing_go_fragment_leaves_both_go_checks_unpassed_and_fails_the_evidence_verdict(tmp_path):
    import subprocess
    from scripts.tiden_ci import assemble
    root = Path(__file__).resolve().parents[1]
    out = tmp_path / "ci-evidence"
    merged = assemble(out, evidence_fragments(tmp_path, go=False), ATTEMPT_URL, PASSED)
    assert merged["missing"] == ["go"] and "go" not in merged["fragments"]
    assert (merged["module_checks"]["whatsmeow"], merged["module_checks"]["release_router"]) == ("skipped", "skipped")
    rows = assembled_rows(tmp_path, merged)
    assert (rows["deedkit-ci::whatsmeow"], rows["deedkit-ci::release_router"]) == ("blocked", "blocked")
    assert rows["deedkit-ci::load"] == "passed"  # the other fragments still count
    def verdict(directory):
        return subprocess.run([sys.executable, str(root / "scripts/tiden_ci.py"), "verdict", "--directory", str(directory)],
                              capture_output=True, text=True)
    # The synthetic verify fragment lacks some module reports, so compare with the same evidence plus the go fragment.
    with_go = tmp_path / "with-go"
    assembled_rows(with_go, assemble(with_go / "ci-evidence", evidence_fragments(with_go), ATTEMPT_URL, PASSED))
    blocked = sum(status == "blocked" for status in rows.values())
    assert verdict(out).returncode == 1 and f"records {blocked} blocked check(s)" in verdict(out).stdout
    assert f"records {blocked - 2} blocked check(s)" in verdict(with_go / "ci-evidence").stdout
    # A go job that failed in this attempt leaves only an earlier attempt's fragment: still never passed.
    stale = evidence_fragments(tmp_path / "stale")
    (stale / "ci-evidence-go" / "attempt").write_text("1\n")
    merged = assemble(tmp_path / "stale" / "ci-evidence", stale, ATTEMPT_URL, {**PASSED, "go": ("failure", "a" * 40)})
    assert merged["missing"] == ["go"] and merged["module_checks"]["whatsmeow"] == merged["module_checks"]["release_router"] == "failure"
    assert not (tmp_path / "stale" / "ci-evidence" / "whatsmeow.json").exists()


ROOT_NODE = "tests/test_collectors.py::test_session_lock_rejects_directory_chowned_to_foreign_uid_as_root"
AUDIO_NODES = ("tests/test_audio_native.py::test_optional_cpu_package_loads_without_gpu_or_model_download",
               "tests/test_audio_native.py::test_faster_whisper_decoder_compatibility_without_model[wav]",
               "tests/test_audio_native.py::test_faster_whisper_decoder_compatibility_without_model[ogg]")


def node_report(*cases):
    """xunit1 JUnit of `file::name` nodes; a case may carry a result element, as in ("node", "<skipped/>")."""
    cases = [case if isinstance(case, tuple) else (case, "") for case in cases]
    return "<testsuite>" + "".join(
        '<testcase classname="{0}" name="{1}" file="{2}">{3}</testcase>'.format(
            node.split("::")[0][:-3].replace("/", "."), node.split("::")[1], node.split("::")[0], result)
        for node, result in cases) + "</testsuite>"


def module_rows(tmp_path, reports, outcome="success"):
    """The CI rows `prepare` records for the module reports given as {module: JUnit text}."""
    meta = {**metadata(), "module_checks": dict.fromkeys(MODULE_REPORTS, outcome)}
    for module, xml in reports.items():
        (tmp_path / MODULE_REPORTS[module][0]).write_text(xml)
    prepare(tmp_path, tmp_path, meta)
    return {row["signature"]: row for row in json.loads((tmp_path / "ci-checks.json").read_text())}


# spec: ANYM-213
def test_the_shared_contract_package_publishes_its_own_module_report_under_its_repository_path(tmp_path):
    # Its tests ran inside the gateway step without a report, so Tiden had no case to link them to (sync B `links`).
    import yaml
    assert MODULE_REPORTS["platform_contracts"] == ("platform-contracts.xml", "python", "packages/platform-contracts")
    assert "platform_contracts" in FRAGMENTS["verify"][0] and "platform-contracts.xml" in FRAGMENTS["verify"][1]
    root = Path(__file__).resolve().parents[1]
    steps = yaml.safe_load((root / ".github/workflows/checks.yml").read_text())["jobs"]["verify"]["steps"]
    step = next(step for step in steps if step.get("id") == "platform_contracts")
    assert step["working-directory"] == "packages/platform-contracts"
    assert '--junitxml="$RUNNER_TEMP/ci-evidence/platform-contracts.xml"' in step["run"]
    assert "platform-contracts/tests" not in next(step for step in steps if step.get("id") == "gateway")["run"]
    rows = module_rows(tmp_path, {"platform_contracts": node_report("tests/test_agent.py::test_valid_results_pass")})
    assert rows["deedkit-ci::platform_contracts"]["execution"]["status"] == "passed"
    case = ET.fromstring((tmp_path / "normalized-platform_contracts.xml").read_bytes()).find("testcase")
    assert (case.get("file"), case.get("classname")) == ("packages/platform-contracts/tests/test_agent.py",
                                                         "packages.platform-contracts.tests.test_agent")

# spec: ANYM-429
def test_the_root_only_and_native_audio_modules_require_exactly_their_root_suite_nodes():
    assert REQUIRED_EXECUTIONS == {"root_session_lock": {ROOT_NODE}, "enricher_audio": set(AUDIO_NODES)}
    assert MODULE_REPORTS["root_session_lock"] == ("root-session-lock.xml", "python", "")
    assert MODULE_REPORTS["enricher_audio"] == ("enricher-audio.xml", "python", "")
    assert {"root_session_lock", "enricher_audio"} <= set(FRAGMENTS["verify"][0])
    assert {"root-session-lock.xml", "enricher-audio.xml"} <= set(FRAGMENTS["verify"][1])


# spec: ANYM-429
@pytest.mark.parametrize("module, cases, status", [
    ("enricher_audio", AUDIO_NODES, "passed"),
    ("enricher_audio", AUDIO_NODES[:1], "failed"),
    ("enricher_audio", (*AUDIO_NODES[:2], (AUDIO_NODES[2], "<skipped/>")), "failed"),
    ("enricher_audio", (*AUDIO_NODES, "tests/test_audio_native.py::test_invalid_native_audio_does_not_reach_provider"), "failed"),
    ("enricher_audio", (*AUDIO_NODES, AUDIO_NODES[0]), "failed"),
    ("enricher_audio", (*AUDIO_NODES[:2], (AUDIO_NODES[2], "<failure/>")), "failed"),
    ("enricher_audio", None, "blocked"),
    ("root_session_lock", (ROOT_NODE,), "passed"),
    ("root_session_lock", ((ROOT_NODE, "<skipped/>"),), "failed"),
    ("root_session_lock", None, "blocked"),
], ids=["exact", "one-case", "pass-plus-skip", "extra-case", "duplicate", "failed-case", "missing-report",
        "root-exact", "root-skipped", "root-missing-report"])
def test_a_root_suite_node_module_passes_only_with_exactly_its_executions_each_once_and_passed(tmp_path, module, cases,
                                                                                             status):
    rows = module_rows(tmp_path, {} if cases is None else {module: node_report(*cases)})
    row = rows["deedkit-ci::" + module]
    assert row["execution"]["status"] == status, row["message"]
    if status == "blocked":  # a missing report keeps today's incomplete verification
        assert row["message"] == "Missing or invalid test report; verification incomplete"
    # The rule holds whatever the step reported: a failed step never reads as passed either.
    if cases is not None:
        (tmp_path / "failed-step").mkdir()
        rows = module_rows(tmp_path / "failed-step", {module: node_report(*cases)}, "failure")
        assert rows["deedkit-ci::" + module]["execution"]["status"] == "failed"


# spec: ANYM-429
def test_assembly_refuses_evidence_whose_shard_report_holds_a_node_only_its_module_check_may_run(tmp_path):
    from scripts.tiden_ci import assemble
    source = tmp_path / "checkout"
    (source / "tests").mkdir(parents=True)
    for file in {node.split("::")[0] for node in (ROOT_NODE, *AUDIO_NODES)}:  # xunit2 places a file by its classname
        (source / file).write_text("")
    # Each spelling of the node's path that shard validation accepts, placed in the checkout as prepare places it.
    for index, (node, spelling) in enumerate([(ROOT_NODE, ""), (AUDIO_NODES[2], ""), (ROOT_NODE, "./"),
                                               (AUDIO_NODES[0], f"{source}/"), (AUDIO_NODES[1], "classname")]):
        fragments = evidence_fragments(tmp_path / str(index))
        report = node_report(node, "tests/x.py::kept")
        file = node.split("::")[0]
        report = report.replace(f' file="{file}"', "") if spelling == "classname" else report.replace(
            f'file="{file}"', f'file="{spelling}{file}"')
        (fragments / "ci-evidence-python-3" / "python-3.xml").write_text(report)
        with pytest.raises(ValueError, match="only its own module check"):
            assemble(tmp_path / str(index) / "ci-evidence", fragments, ATTEMPT_URL, PASSED, source_root=source)
    # Without such a node the same fragments assemble, and the module reports of verify read as passed.
    fragments = evidence_fragments(tmp_path / "clean", verify_files=("root-session-lock.xml", "enricher-audio.xml"))
    (fragments / "ci-evidence-verify" / "root-session-lock.xml").write_text(node_report(ROOT_NODE))
    (fragments / "ci-evidence-verify" / "enricher-audio.xml").write_text(node_report(*AUDIO_NODES))
    rows = assembled_rows(tmp_path / "clean", assemble(tmp_path / "clean" / "ci-evidence", fragments, ATTEMPT_URL, PASSED))
    assert rows["deedkit-ci::root_session_lock"] == rows["deedkit-ci::enricher_audio"] == "passed"


def verify_steps():
    import yaml
    root = Path(__file__).resolve().parents[1]
    return yaml.safe_load((root / ".github/workflows/checks.yml").read_text())["jobs"]


def stub_tools(tmp_path):
    """`docker`, `sudo` and the checkout's `.venv/bin/python` stubs that record each call in $CALLS; a test run writes
    its JUnit report to the `--junitxml` path (docker: under the mounted evidence directory) and exits $STUB_EXIT."""
    tools = tmp_path / "bin"
    tools.mkdir()
    (tools / "docker").write_text('''#!/bin/sh
echo "docker $*" >> "$CALLS"
if [ "$1" = run ]; then
  evidence=""
  for arg; do case "$previous" in --volume) evidence="${arg%%:*}";; esac; previous="$arg"; done
  echo '<testsuite/>' > "$evidence/enricher-audio.xml"
  exit "${STUB_EXIT:-0}"
fi
''')
    (tools / "sudo").write_text('#!/bin/sh\necho "sudo $*" >> "$CALLS"\nexec "$@"\n')
    python = '''#!/bin/sh
echo "python $*" >> "$CALLS"
case "$*" in *--collect-only*) grep -q "def ${ROOT_NODE##*::}(" tests/test_collectors.py; exit $?;; esac
for arg; do case "$arg" in --junitxml=*) echo '<testsuite/>' > "${arg#--junitxml=}";; esac; done
exit "${STUB_EXIT:-0}"
'''
    for path in (tools / "docker", tools / "sudo"):
        path.chmod(0o755)
    return tools, python


def fixture_source(tmp_path, python, *, node=True, target=True):
    """A tested checkout with or without the root-only node and the enricher-test Dockerfile target."""
    source = tmp_path / "source"
    (source / "tests").mkdir(parents=True)
    (source / ".venv" / "bin").mkdir(parents=True)
    (source / ".venv" / "bin" / "python").write_text(python)
    (source / ".venv" / "bin" / "python").chmod(0o755)
    (source / "tests" / "test_collectors.py").write_text(
        "def test_session_lock_rejects_directory_chowned_to_foreign_uid_as_root(tmp_path):\n    pass\n" if node else
        "def test_session_lock_creates_missing_parent_private_and_keeps_symlink_code(tmp_path):\n    pass\n")
    (source / "Dockerfile").write_text("FROM enricher-local AS enricher-test\n" if target else "FROM enricher-local AS enricher-local\n")
    return source


def run_step(step, source, tools, temp, **env):
    import subprocess
    (temp / "ci-evidence").mkdir(parents=True, exist_ok=True)
    return subprocess.run(["bash", "-e", "-c", step["run"]], cwd=source, capture_output=True, text=True, timeout=30,
                          env={"PATH": f"{tools}:/usr/bin:/bin:/usr/sbin", "RUNNER_TEMP": str(temp), "CALLS": str(temp / "calls"),
                               **{key: str(value) for key, value in (step.get("env") or {}).items()}, **env})


def named(steps, name):
    return next(step for step in steps if step.get("name") == name)


START_AUDIO = "Start the native audio test image build in the background"
SHARED_WAIT = "Wait for the background router image and Python image"


# spec: ANYM-429
def test_the_native_audio_step_waits_only_for_its_own_build_and_keeps_its_test_status_and_report(tmp_path):
    import time
    steps = verify_steps()["verify"]["steps"]
    ids = [step.get("id") for step in steps]
    audio = steps[ids.index("enricher_audio")]
    assert ids.index("enricher_audio") > ids.index("python") and ids.index("root_session_lock") > ids.index("python")
    assert audio["if"] == "${{ !cancelled() && steps.build.outcome == 'success' }}" and "AUDIO_IMAGE_WAIT_SECONDS" in audio["env"]
    names = [step.get("name") for step in steps]
    assert names.index(START_AUDIO) < names.index("Gateway isolated package and transport tests")
    tools, python = stub_tools(tmp_path)
    source = fixture_source(tmp_path, python)
    temp = tmp_path / "runner"
    background = temp / "background"
    background.mkdir(parents=True)
    # (a) A failed build fails the step, and so does a build that never finished within the step's own timeout.
    (background / "enricher-test.exit").write_text("1\n")
    assert run_step(audio, source, tools, temp, AUDIO_IMAGE_WAIT_SECONDS="1").returncode != 0
    (background / "enricher-test.exit").unlink()
    began = time.monotonic()
    assert run_step(audio, source, tools, temp, AUDIO_IMAGE_WAIT_SECONDS="1").returncode != 0
    assert time.monotonic() - began < 10 and not (temp / "calls").exists()
    # (b) The shared wait of the Python shard reads only the router and Python image results, never this build.
    (background / "router.exit").write_text("0\n")
    (background / "python-image.exit").write_text("0\n")
    (background / "enricher-test.exit").write_text("1\n")
    assert run_step(named(steps, SHARED_WAIT), source, tools, temp, BACKGROUND_WAIT_SECONDS="1").returncode == 0
    assert "enricher" not in named(steps, SHARED_WAIT)["run"]
    # (c) A test run that writes its report and fails fails the step, and the report stays for the evidence.
    (background / "enricher-test.exit").write_text("0\n")
    failed = run_step(audio, source, tools, temp, STUB_EXIT="1")
    assert failed.returncode != 0 and (temp / "ci-evidence" / "enricher-audio.xml").is_file()
    (temp / "ci-evidence" / "enricher-audio.xml").unlink()
    assert run_step(audio, source, tools, temp).returncode == 0 and (temp / "ci-evidence" / "enricher-audio.xml").is_file()
    runs = [line for line in (temp / "calls").read_text().splitlines() if line.startswith("docker run")]
    assert len(runs) == 2
    for flags in ("--network none", "--read-only", "--tmpfs /tmp", "--user ", "deedkit-enricher-test:ci",
                  "--junitxml=/evidence/enricher-audio.xml", "-o junit_family=xunit1", "-p no:cacheprovider"):
        assert flags in runs[-1], flags
    assert {node.split("[")[0] for node in AUDIO_NODES} == {arg for arg in runs[-1].split() if arg.startswith("tests/")}


# spec: ANYM-429
def test_the_root_step_runs_only_its_node_under_sudo_and_hands_the_report_back_after_a_failing_run(tmp_path):
    steps = verify_steps()["verify"]["steps"]
    root_step = next(step for step in steps if step.get("id") == "root_session_lock")
    assert root_step["if"] == "${{ !cancelled() && steps.build.outcome == 'success' }}"
    assert root_step["env"]["ROOT_NODE"] == ROOT_NODE and str(root_step["env"]["PYTHONDONTWRITEBYTECODE"]) == "1"
    # The backup restore step keeps its own condition.
    assert next(step for step in steps if step.get("id") == "backup_restore")["if"] == root_step["if"]
    tools, python = stub_tools(tmp_path)
    source = fixture_source(tmp_path, python)
    temp = tmp_path / "runner"
    report = temp / "ci-evidence" / "root-session-lock.xml"
    # (c), (d) A failing test run fails the step, its report stays, and sudo hands the report back to the runner user.
    failed = run_step(root_step, source, tools, temp, STUB_EXIT="1")
    assert failed.returncode == 1 and report.is_file(), failed.stderr
    calls = (temp / "calls").read_text().splitlines()
    run = next(line for line in calls if line.startswith("sudo env"))
    for flags in ("PYTHONDONTWRITEBYTECODE=1", "-p no:cacheprovider", "-o junit_family=xunit1", f"--junitxml={report}"):
        assert flags in run, flags
    assert run.endswith(ROOT_NODE)
    assert calls[-1].startswith("sudo chown ") and calls[-1].endswith(str(report))
    report.unlink()
    assert run_step(root_step, source, tools, temp).returncode == 0 and report.is_file()


# spec: ANYM-429
def test_a_tested_source_without_the_root_node_or_the_enricher_target_runs_neither_and_reads_blocked(tmp_path):
    import time
    steps = verify_steps()["verify"]["steps"]
    start = named(steps, START_AUDIO)
    root_step, audio = (next(step for step in steps if step.get("id") == name) for name in ("root_session_lock", "enricher_audio"))
    tools, python = stub_tools(tmp_path)
    for present in (False, True):
        work = tmp_path / str(present)
        source = fixture_source(work, python, node=present, target=present)
        temp = work / "runner"
        assert run_step(start, source, tools, temp).returncode == 0
        for _ in range(100):  # the background build is a stub that finishes at once
            if not present or (temp / "background" / "enricher-test.exit").exists():
                break
            time.sleep(0.1)
        assert run_step(root_step, source, tools, temp).returncode == 0
        assert run_step(audio, source, tools, temp, AUDIO_IMAGE_WAIT_SECONDS="5").returncode == 0
        calls = (temp / "calls").read_text().splitlines() if (temp / "calls").exists() else []
        runs = (sum(line.startswith("docker build") for line in calls), sum(line.startswith("sudo env") for line in calls),
                sum(line.startswith("docker run") for line in calls))
        reports = {path.name for path in (temp / "ci-evidence").iterdir()}
        if present:
            assert runs == (1, 1, 1) and reports == {"root-session-lock.xml", "enricher-audio.xml"}
            assert "--target enricher-test" in next(line for line in calls if line.startswith("docker build"))
            continue
        assert runs == (0, 0, 0) and reports == set() and not any("--junitxml" in line for line in calls)
        rows = module_rows(temp / "ci-evidence", {})
        for module in ("root_session_lock", "enricher_audio"):
            assert rows["deedkit-ci::" + module]["execution"]["status"] == "blocked"


# spec: ANYM-429
def test_both_shard_lines_deselect_exactly_the_nodes_the_module_steps_run():
    import re
    jobs = verify_steps()
    nodes = {node.split("[")[0] for required in REQUIRED_EXECUTIONS.values() for node in required}
    for job in ("verify", *SHARD_JOBS):
        step = next(step for step in jobs[job]["steps"] if step.get("id") == "python")
        assert set(re.findall(r"--deselect (\S+)", step["run"])) == nodes, job
        assert step["run"].rstrip().endswith(" tests")
    # pytest deselects by node ID prefix, so no other test of these files may start with a deselected name.
    root = Path(__file__).resolve().parents[1]
    for node in nodes:
        file, name = node.split("::")
        assert [test for test in re.findall(r"^def (test_\w+)", (root / file).read_text(), re.MULTILINE)
                if test.startswith(name)] == [name]
    outcomes = next(step for step in jobs["verify"]["steps"] if step.get("name") == "Prepare honest CI evidence")["env"]
    pairs = re.findall(r'"(\w+)":"\$\{\{ steps\.(\w+)\.outcome \}\}"', outcomes["MODULE_OUTCOMES"])
    assert dict(pairs) == {name: name for name in FRAGMENTS["verify"][0]} and len(pairs) == len(FRAGMENTS["verify"][0])


# spec: ANYM-154
@pytest.mark.parametrize("pin, installs", [
    ("TIDEN_CLI_VERSION=0.42.0\nTIDEN_CLI_SHA256_linux_amd64=" + "a" * 64 + "\n", True),
    ("TIDEN_CLI_VERSION=0/../../../../../OWNER/REPO/releases/download/v1\nTIDEN_CLI_SHA256_linux_amd64="
     + "a" * 64 + "\n", False),
    ("TIDEN_CLI_VERSION=0.42.0 \nTIDEN_CLI_SHA256_linux_amd64=" + "a" * 64 + "\n", False),
    ("TIDEN_CLI_VERSION=0.42.0\nTIDEN_CLI_SHA256_linux_amd64=" + "A" * 64 + "\n", False),
    ("TIDEN_CLI_VERSION=0.42.0\nTIDEN_CLI_SHA256_linux_amd64=" + "a" * 63 + "\n", False),
    ("TIDEN_CLI_VERSION=0.42.0\n", False),
    ("", False),
], ids=["valid", "path-escape", "trailing-space", "upper-hex", "short-checksum", "no-checksum", "empty"])
def test_every_tiden_cli_install_accepts_only_a_well_formed_shared_pin(tmp_path, pin, installs):
    import os
    import subprocess
    import yaml
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "docs/harness").mkdir(parents=True)
    (tmp_path / "docs/harness/tiden.md").write_text("# Tiden CLI version\n\n" + pin)
    scripts = [step["run"] for path in sorted((root / ".github/workflows").glob("*.yml"))
               for job in yaml.safe_load(path.read_text())["jobs"].values() for step in job.get("steps", [])
               if "docs/harness/tiden.md" in step.get("run", "")]
    assert len(scripts) == 8
    for script in scripts:
        lines = script.splitlines()
        check = next(index for index, line in enumerate(lines) if line.startswith("[ -n \"$ver\" ]"))
        # Every download names the vendor's release by the read version; every checksum check is strict.
        assert all("releases/download/v$ver/" in line for line in lines if "releases/download" in line)
        assert all("--strict" in line for line in lines if "sha256sum" in line)
        result = subprocess.run(["bash", "-e", "-c", "\n".join(lines[:check + 1]) + '\necho "$ver $sum"'],
                                capture_output=True, text=True,
                                env={"PATH": os.environ["PATH"], "GITHUB_WORKSPACE": str(tmp_path)})
        if installs:
            assert (result.returncode, result.stdout) == (0, "0.42.0 " + "a" * 64 + "\n")
        else:
            assert result.returncode == 1 and "no valid Tiden pin" in result.stderr and not result.stdout
    # The setup script reads the same pin with the same expressions.
    setup = (root / "scripts/tiden_agent_setup.sh").read_text()
    for expression in ("s/^TIDEN_CLI_VERSION=\\([0-9][0-9]*\\.[0-9][0-9]*\\.[0-9][0-9]*\\)$/\\1/p",
                       "s/^TIDEN_CLI_SHA256_linux_amd64=\\([0-9a-f]\\{64\\}\\)$/\\1/p"):
        assert all(expression in script for script in scripts) and expression in setup
