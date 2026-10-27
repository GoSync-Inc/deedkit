"""`deedkit admin profile tools` over a synthetic host: tests/deedkit_admin_fakes.py. Shared rules: ANYM-493."""
import json
import os

import pytest

from scripts import deedkit_admin as admin
from scripts import seamless_release as s
from tests.deedkit_admin_fakes import Site, private

BASE = ["platform_capabilities", "messages_capabilities", "messages_dismiss_reply"]
RESTORE, MUTE = "messages_restore_reply", "messages_mute_reply_source"


class Crash(BaseException):
    pass


@pytest.fixture
def site(tmp_path, monkeypatch):
    umask = os.umask(0o022)
    os.umask(umask)
    site = Site(tmp_path, monkeypatch, admin, s)
    doc = site.policy()  # a second, narrower profile next to `full`, used by the bystander `other`
    doc["profiles"]["reader"] = {"tools": list(BASE), "actions": {"messenger_operations": ["archive_chat"]}}
    next(row for row in doc["principals"] if row["id"] == "other")["profile"] = "reader"
    private(site.gw / "policy.json", doc)
    yield site
    os.umask(umask)


def call(site, capsys, *argv):
    code = site.main(*argv)
    out, err = capsys.readouterr()
    return code, (json.loads(out) if code == 0 else None), err


def tools(site, capsys, *extra, profile="reader"):
    return call(site, capsys, "profile", "tools", profile, *extra)


def unchanged(site, capsys, message, *extra, profile="reader"):
    before = site.snapshot()
    code, _, err = tools(site, capsys, *extra, profile=profile)
    assert code == 2 and message in err and "nothing changed" in err, err
    assert site.snapshot() == before and not (site.root / "ops").exists()


# spec: ANYM-493
def test_profile_tools_adds_only_the_named_tools_and_bumps_the_revision(site, capsys):
    before = site.policy()
    code, result, err = tools(site, capsys, "--add", RESTORE, MUTE)
    assert code == 0, err
    after = site.policy()
    assert after["profiles"]["reader"]["tools"] == [*BASE, RESTORE, MUTE]
    assert after["profiles"]["reader"]["actions"] == before["profiles"]["reader"]["actions"]
    assert after["revision"] == before["revision"] + 1
    assert after["profiles"]["full"] == before["profiles"]["full"] and after["principals"] == before["principals"]
    assert {k: v for k, v in after.items() if k not in ("revision", "profiles")} == {
        k: v for k, v in before.items() if k not in ("revision", "profiles")}
    assert result["added"] == [RESTORE, MUTE] and result["removed"] == [] and result["revision"] == after["revision"]
    assert result["principals"] == [{"principal": "other", "enabled": True}]
    assert result["undo"] == f"deedkit admin undo {result['operation']}"
    assert site.journal(result["operation"])["state"] == "done"


# spec: ANYM-493
def test_profile_tools_removes_and_leaves_a_listed_tool_alone(site, capsys):
    code, result, err = tools(site, capsys, "--add", "messages_dismiss_reply", RESTORE, "--remove", "messages_capabilities")
    assert code == 0, err
    assert site.policy()["profiles"]["reader"]["tools"] == ["platform_capabilities", "messages_dismiss_reply", RESTORE]
    assert result["added"] == [RESTORE] and result["removed"] == ["messages_capabilities"]


# spec: ANYM-493
def test_profile_tools_with_nothing_to_change_writes_no_revision(site, capsys):
    before = site.policy()
    code, result, err = tools(site, capsys, "--add", "messages_dismiss_reply", "--remove", MUTE)
    assert code == 0, err
    assert site.policy() == before and result["added"] == [] and result["removed"] == []


# spec: ANYM-493
def test_profile_tools_dry_run_writes_nothing_and_lists_the_principals_it_reaches(site, capsys):
    before = site.snapshot()
    code, plan, err = tools(site, capsys, "--add", RESTORE, "messages_dismiss_reply", "--dry-run")
    assert code == 0, err
    assert plan["dry_run"] and plan["added"] == [RESTORE] and plan["already_listed"] == ["messages_dismiss_reply"]
    assert plan["principals"] == [{"principal": "other", "enabled": True}]
    assert site.snapshot() == before and not (site.root / "ops").exists()


# spec: ANYM-493
@pytest.mark.parametrize("extra", [
    ["--add", "Bad Name"], ["--add", RESTORE, "--remove", RESTORE], ["--add", RESTORE, RESTORE], []],
    ids=["name", "overlap", "duplicate", "nothing"])
def test_profile_tools_refuses_bad_arguments_before_any_write(site, capsys, extra):
    unchanged(site, capsys, "give --add and/or --remove", *extra)


# spec: ANYM-493
def test_profile_tools_refuses_an_unknown_profile(site, capsys):
    unchanged(site, capsys, "no gateway profile 'nope'", "--add", RESTORE, profile="nope")


# spec: ANYM-493
def test_profile_tools_refuses_a_tool_the_rollback_image_does_not_know(site, capsys):
    # Rule (c): the candidate is checked by `gateway:old`, the image `production.py rollback` would restore.
    unchanged(site, capsys, "refused by image gateway:old", "--add", "messages_no_such_tool")


# spec: ANYM-493
def test_profile_tools_refuses_while_a_call_is_active(site, capsys):
    site.calls = ["call-1 (in_progress)"]
    unchanged(site, capsys, "a call is active (call-1 (in_progress))", "--add", RESTORE)


# spec: ANYM-493
def test_profile_tools_refuses_a_gateway_that_enforces_another_policy_file(site, capsys):
    site.gateway["Config"]["Cmd"] = ["--policy", "/etc/deedkit-gateway/active.json", "--port", "13240"]
    unchanged(site, capsys, "not the configured policy", "--add", RESTORE)


# spec: ANYM-493
def test_profile_tools_refuses_when_the_policy_changed_during_the_operation(site, capsys, monkeypatch):
    real = admin.validate

    def validate(config, docker, kind, staging, found, extra, outcome="not renamed"):
        real(config, docker, kind, staging, found, extra, outcome)
        if outcome == "not renamed":  # the compare-and-swap's check, after the candidate was validated
            doc = site.policy()
            doc["revision"] += 1
            private(site.gw / "policy.json", doc)
    monkeypatch.setattr(admin, "validate", validate)
    code, _, err = tools(site, capsys, "--add", RESTORE)
    assert code == 1 and "changed during this operation; not renamed" in err, err
    assert RESTORE not in site.policy()["profiles"]["reader"]["tools"]


# spec: ANYM-493
def test_undo_puts_back_the_exact_tool_list(site, capsys):
    before = site.policy()["profiles"]["reader"]
    code, result, err = tools(site, capsys, "--add", RESTORE, "--remove", "platform_capabilities")
    assert code == 0, err
    code, undone, err = call(site, capsys, "undo", result["operation"])
    assert code == 0, err
    assert site.policy()["profiles"]["reader"] == before
    assert undone["restored"] and undone["taken_out"] == [RESTORE] and undone["added_back"] == ["platform_capabilities"]
    assert site.policy()["revision"] == 9  # 7, the change 8, the undo 9: revisions never go back
    code, again, err = call(site, capsys, "undo", result["operation"])
    assert code == 0 and again["undone"] and site.policy()["revision"] == 9


# spec: ANYM-493
def test_undo_refuses_a_profile_someone_else_changed_since(site, capsys):
    code, result, err = tools(site, capsys, "--add", RESTORE)
    assert code == 0, err
    assert tools(site, capsys, "--add", MUTE)[0] == 0
    before = site.snapshot()
    code, _, err = call(site, capsys, "undo", result["operation"])
    assert code == 2 and "changed since this operation wrote it; nothing changed" in err, err
    assert site.snapshot() == before


# spec: ANYM-493
@pytest.mark.parametrize("point", [("gateway policy", "renamed")])
def test_a_failure_after_the_rename_is_finished_by_a_rerun_without_a_second_revision(site, capsys, monkeypatch, point):
    def checkpoint(step, phase):
        if (step, phase) == point:
            raise Crash(point)
    monkeypatch.setattr(admin, "checkpoint", checkpoint)
    with pytest.raises(Crash):
        site.main("profile", "tools", "reader", "--add", RESTORE)
    monkeypatch.setattr(admin, "checkpoint", lambda step, phase: None)
    (path,) = (site.root / "ops").glob("*.json")
    code, result, err = tools(site, capsys, "--add", RESTORE, "--operation", path.stem)
    assert code == 0, err
    assert site.policy()["profiles"]["reader"]["tools"] == [*BASE, RESTORE] and site.policy()["revision"] == 8
    assert result["added"] == [RESTORE] and site.journal(path.stem)["state"] == "done"


# spec: ANYM-493
def test_a_rerun_with_other_arguments_is_refused(site, capsys, monkeypatch):
    monkeypatch.setattr(admin, "checkpoint", lambda step, phase: (_ for _ in ()).throw(Crash()) if phase == "renamed" else None)
    with pytest.raises(Crash):
        site.main("profile", "tools", "reader", "--add", RESTORE)
    (path,) = (site.root / "ops").glob("*.json")
    code, _, err = tools(site, capsys, "--add", MUTE, "--operation", path.stem)
    assert code == 2 and "started with other arguments" in err, err


# spec: ANYM-493
def test_a_resumed_operation_never_writes_over_a_profile_changed_after_its_plan(site, capsys, monkeypatch):
    original = admin.Journal.create

    def crash_after_create(journal, data):
        original(journal, data)
        raise Crash()
    with monkeypatch.context() as patch:
        patch.setattr(admin.Journal, "create", crash_after_create)
        with pytest.raises(Crash):
            site.main("profile", "tools", "reader", "--add", RESTORE)
    (path,) = (site.root / "ops").glob("*.json")
    assert tools(site, capsys, "--remove", "messages_dismiss_reply")[0] == 0  # a second operation, after the plan
    code, _, err = tools(site, capsys, "--add", RESTORE, "--operation", path.stem)
    assert code == 1 and "changed since this operation read it" in err, err
    assert site.policy()["profiles"]["reader"]["tools"] == ["platform_capabilities", "messages_capabilities"]


# spec: ANYM-493
def test_undo_refuses_a_gateway_that_enforces_another_policy_file(site, capsys):
    code, result, err = tools(site, capsys, "--add", RESTORE)
    assert code == 0, err
    site.gateway["Config"]["Cmd"] = ["--policy", "/etc/deedkit-gateway/active.json", "--port", "13240"]
    before = site.snapshot()
    code, _, err = call(site, capsys, "undo", result["operation"])
    assert code == 2 and "not the configured policy" in err, err
    assert site.snapshot() == before


# spec: ANYM-493
def test_profile_tools_checks_the_live_policy_in_every_image_before_the_candidate(site, capsys):
    # The rollback image rejects the policy as it is now (it lists platform_capabilities) but would accept its removal.
    site.docker.images["gateway:old"] = lambda path: "platform_capabilities" not in json.loads(
        path.read_text())["profiles"]["reader"]["tools"]
    unchanged(site, capsys, "refused by image gateway:old", "--remove", "platform_capabilities")


# spec: ANYM-493
def test_an_interrupted_undo_is_finished_and_reports_what_it_restored(site, capsys, monkeypatch):
    code, result, err = tools(site, capsys, "--add", RESTORE)
    assert code == 0, err
    monkeypatch.setattr(admin, "checkpoint", lambda step, phase: (_ for _ in ()).throw(Crash())
                        if (step, phase) == ("undo gateway policy", "renamed") else None)
    with pytest.raises(Crash):
        site.main("undo", result["operation"])
    monkeypatch.setattr(admin, "checkpoint", lambda step, phase: None)
    code, undone, err = call(site, capsys, "undo", result["operation"])
    assert code == 0, err
    assert site.policy()["profiles"]["reader"]["tools"] == BASE
    assert undone["restored"] and undone["taken_out"] == [RESTORE]
    assert site.journal(result["operation"])["steps"]["undo gateway policy"]["state"] == "done"
