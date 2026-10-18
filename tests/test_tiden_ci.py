"""Reporting tests use synthetic JUnit and never call Tiden."""
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

import pytest

from scripts.tiden_ci import (CHECKS, intent_links, linked_intent_branch, normalize_junit, prepare, publish,
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
    _, manifest = cli_fixture(monkeypatch, tmp_path, attempts="13")
    manifest["module_checks"] = {name: "success" for name in ("gateway", "calendar", "calls", "calls_frontend", "load", "backup_restore")}
    manifest["check_counts"] = {"passed": 13}
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
    assert not any("if" in ci["jobs"][job] for job in ("native", "images"))
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
    assert steps["actions/checkout"]["if"] == steps["actions/download-artifact"]["if"] == enabled_or_queue
    # Only a configured non-queue run installs the CLI and receives the credential.
    for name in ("Install pinned Tiden CLI with integrity check", "Publish and verify branch association"):
        assert steps[name]["if"] == "steps.config.outputs.enabled == 'true'"
    queue = steps["Record that a merge queue run publishes nothing"]
    assert queue["if"] == "steps.config.outputs.queue == 'true'" and "env" not in queue
    assert queue["run"] == 'python scripts/tiden_ci.py publish --directory "$RUNNER_TEMP/ci-evidence"'
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
    # Other events build without reading Git at all, even outside a checkout.
    for event in ("push", "merge_group", "workflow_dispatch"):
        empty = tmp_path / event
        empty.mkdir()
        assert image_decision(empty, tmp_path, event, head)[0] == ["build=true"]


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
    assert build["steps"][1]["run"] == 'python3 scripts/production.py check-image --target "$TARGET"'
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
    # Only after the CI gate and the image publication, so every verification job, the Tiden
    # publication and the GHCR publication passed.
    assert job["needs"] == ["gate", "publish"]
    assert job["if"] == "github.event_name == 'push' && github.ref == 'refs/heads/main'"
    steps = {step.get("name") or step["uses"].split("@")[0]: step for step in job["steps"]}
    record = steps["Record the main release in Tiden"]
    assert record["if"] == "steps.config.outputs.enabled == 'true'" and "continue-on-error" not in record
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
    # The Tiden token reaches only the release step, and only from GitHub secrets.
    holders = [step for step in steps if "TIDEN_API_TOKEN" in json.dumps(step)]
    assert holders == [steps[publish]]
    assert steps[publish]["env"]["TIDEN_API_TOKEN"] == "${{ secrets.TIDEN_API_TOKEN }}"
    assert steps[publish]["env"]["ENVIRONMENT"] == "${{ inputs.environment }}"
    assert "${{" not in steps[verify]["run"] and "${{" not in steps[publish]["run"]


ATTEMPT_URL = "https://github.com/example/repo/actions/runs/1/attempts/2"
LOAD_REPORT = '<testsuite><testcase name="budgets" file="tests/platform/benchmarks/gateway_read_load.py"/></testsuite>'
PASSED = {"verify": ("success", "a" * 40), "load": ("success", "a" * 40)}


def evidence_fragments(tmp_path, *, load_sha="a" * 40, load_outcomes=None, load=True, load_attempt="2",
                       verify_modules=None, verify_files=(), load_files=()):
    fragments = tmp_path / "fragments"
    verify = fragments / "ci-evidence-verify"
    verify.mkdir(parents=True)
    (verify / "source-sha").write_text("a" * 40 + "\n")
    (verify / "attempt").write_text("2\n")
    modules = verify_modules or {name: "success" for name in ("gateway", "calendar", "calls", "calls_frontend", "backup_restore")}
    (verify / "metadata.json").write_text(json.dumps({**metadata(), "tiden_branch": "", "module_checks": modules}))
    for name in ("python.xml", *verify_files):
        (verify / name).write_text('<testsuite><testcase name="x" file="tests/x.py"/></testsuite>')
    if load:
        fragment = fragments / "ci-evidence-load"
        fragment.mkdir()
        (fragment / "source-sha").write_text(load_sha + "\n")
        (fragment / "attempt").write_text(load_attempt + "\n")
        for name in ("load.xml", *load_files):
            (fragment / name).write_text(LOAD_REPORT)
        (fragment / "load.json").write_text('{"status": "passed"}')
        (fragment / "outcomes.json").write_text(json.dumps(load_outcomes or {"module_checks": {"load": "success"}}))
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
    assert merged["ci_url"] == ATTEMPT_URL and merged["fragments"] == ["verify", "load"] and merged["missing"] == []
    assert merged["module_checks"]["load"] == "success" and set(merged["module_checks"]) == {
        "gateway", "calendar", "calls", "calls_frontend", "load", "backup_restore"}
    assert {path.name for path in out.iterdir()} == {"metadata.json", "source-sha", "python.xml", "load.xml", "load.json"}
    rows = assembled_rows(tmp_path, merged)
    assert rows["deedkit-ci::load"] == rows["anymsg-ci::python"] == "passed"
    # A job that succeeded and was not rerun keeps its earlier attempt's fragment, and the evidence says so.
    earlier = assemble(tmp_path / "earlier", evidence_fragments(tmp_path / "e", load_attempt="1"), ATTEMPT_URL, PASSED)
    assert earlier["fragments"] == ["verify", "load"]
    assert earlier["producers"] == {"verify": {"result": "success", "sha": "a" * 40, "attempt": "2"},
                                    "load": {"result": "success", "sha": "a" * 40, "attempt": "1"}}
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
    claimed = {name: "success" for name in ("gateway", "calendar", "calls", "calls_frontend", "backup_restore", "load")}
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
    assert merged["fragments"] == ["verify"] and merged["missing"] == ["load"] and merged["module_checks"]["load"] == "skipped"
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
    assert set(merged["checks"].values()) == {"cancelled"} and not (tmp_path / "v" / "ci-evidence" / "python.xml").exists()
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
                           "LOAD": "load:${{ needs.load.result }}:${{ needs.load.outputs.source_sha }}"}
    work = tmp_path / "work"
    work.mkdir()
    (work / "ci-tools").symlink_to(root, target_is_directory=True)
    temp = tmp_path / "runner"
    temp.mkdir()
    evidence_fragments(temp, load=False)  # both jobs report success, the load artifact is gone, Tiden is unconfigured
    env = {"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin", "RUNNER_TEMP": str(temp), "GITHUB_REPOSITORY": "example/repo",
           "GITHUB_RUN_ID": "1", "GITHUB_RUN_ATTEMPT": "2", "VERIFY": "verify:success:" + "a" * 40,
           "LOAD": "load:success:" + "a" * 40}
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
    assert evidence["needs"] == ["documentation", "verify", "load"] and evidence["if"] == "${{ always() && !inputs.reuse_queue_evidence && !inputs.reuse_pr_verification && needs.documentation.outputs.docs_only != 'true' }}"
    assert jobs["tiden"]["needs"] == ["documentation", "evidence"]
    steps = {step.get("name") or step["uses"].split("@")[0]: step for step in evidence["steps"]}
    assert steps["actions/download-artifact"]["with"]["pattern"] == "ci-evidence-*"
    assert "tiden_ci.py assemble" in steps["Merge the fragments of the tested commit and record every check outcome"]["run"]
    retained = steps["Retain test evidence even when a check fails"]["with"]
    assert retained["name"] == "ci-evidence" and retained["overwrite"] is True  # a rerun attempt replaces it
    require = steps["Require every test job to have succeeded"]
    assert require["if"] == "${{ always() }}"
    assert require["env"] == {"VERIFY_RESULT": "${{ needs.verify.result }}", "LOAD_RESULT": "${{ needs.load.result }}"}
    for verify, load_result in [("success", "success"), ("success", "failure"), ("success", "cancelled"),
                                ("success", "skipped"), ("failure", "success"), ("skipped", "success")]:
        result = subprocess.run(["bash", "-e", "-c", require["run"]], capture_output=True,
                                env={"VERIFY_RESULT": verify, "LOAD_RESULT": load_result})
        assert (result.returncode == 0) == (verify == load_result == "success"), (verify, load_result)


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
