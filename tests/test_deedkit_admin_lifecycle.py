"""`deedkit admin key extend` and `key revoke` (ANYM-497) over a synthetic host: tests/deedkit_admin_fakes.py."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path

import pytest

from scripts import deedkit_admin as admin
from scripts import seamless_release as s
from tests.deedkit_admin_fakes import PAST, Site, private

STORES = ("gateway", "messaging", "messaging-archive", "calendar", "calls")


class Crash(BaseException):
    pass


@pytest.fixture
def site(tmp_path, monkeypatch):
    umask = os.umask(0o022)
    os.umask(umask)
    yield Site(tmp_path, monkeypatch, admin, s)
    os.umask(umask)  # main() sets 0077 for its own files


def call(site, capsys, *argv):
    code = site.main(*argv)
    out, err = capsys.readouterr()
    return code, (json.loads(out) if code == 0 else None), err


def extend(site, capsys, *extra, days="120"):
    return call(site, capsys, "key", "extend", "src", "--days", days, *extra)


def revoke(site, capsys, *extra):
    return call(site, capsys, "key", "revoke", "src", *extra)


def entry(document, name):
    return next((row for row in document["principals"] if row["id"] == name), None)


def expiries(site) -> dict:
    """Every store's expiry of `src` as datetimes."""
    state = site.operator.state
    keys = state["messaging"]["keys"]
    return {name: datetime.fromisoformat(value) for name, value in (
        ("gateway", entry(site.policy(), "src")["expires_at"]),
        ("messaging", keys["key-src"]["expires_at"]),
        ("messaging-archive", keys["key-src-calls-archive"]["expires_at"]),
        ("calendar", state["calendar"]["agents"]["src"]["expires_at"]),
        ("calls", entry(site.registry_doc(), "src")["expires_at"]))}


def pins(site) -> dict:
    """The hashes and key files that bind `src`: an extend keeps every one."""
    gateway = entry(site.policy(), "src")
    return {"gateway": gateway["key_sha256"], "credentials": gateway["credentials"],
            "calls": entry(site.registry_doc(), "src")["key_sha256"],
            "files": {p.name: p.read_bytes() for p in sorted(site.keys.glob("src-*")) + sorted(site.voice.glob("src-*"))}}


def started(site) -> str:
    """The ID of the one operation a stopped command left."""
    (path,) = (site.root / "ops").glob("*.json")
    return path.stem


def crash_at(monkeypatch, point):
    def checkpoint(step, phase):
        if (step, phase) == point:
            raise Crash(point)
    monkeypatch.setattr(admin, "checkpoint", checkpoint)


def unchanged(site, capsys, run, message):
    before = site.snapshot()
    code, _, err = run()
    assert code == 2 and message in err and "nothing changed" in err, err
    assert site.snapshot() == before
    assert site.operator.state["extended"] == [] and not (site.root / "ops").exists()


# key extend

# spec: ANYM-497
def test_extend_moves_every_store_and_ends_the_gateway_at_the_earliest_backend(site, capsys):
    before, bound = expiries(site), pins(site)
    start = datetime.now(timezone.utc)
    code, result, err = extend(site, capsys)
    assert code == 0, err
    after = expiries(site)
    assert all(after[name] > before[name] for name in STORES)
    assert after["gateway"] == min(after[name] for name in STORES if name != "gateway")
    assert start + timedelta(days=120) - timedelta(seconds=5) <= after["gateway"] <= after["messaging"]
    assert pins(site) == bound  # tokens and hashes stay the same
    shown = {row["store"]: row for row in result["stores"]}
    assert set(shown) == set(STORES)
    for name in STORES:
        assert datetime.fromisoformat(shown[name]["previous_expires_at"]) == before[name]
        assert datetime.fromisoformat(shown[name]["expires_at"]) == after[name]
    assert sorted(site.operator.state["extended"]) == ["key-src", "key-src-calls-archive", "src"]
    assert site.policy()["revision"] == 8 and site.registry_doc()["revision"] == 6


# spec: ANYM-497
def test_extend_keeps_the_gateway_at_the_earliest_backend_when_a_step_is_delayed(site, capsys, monkeypatch):
    def checkpoint(step, phase):  # the calendar step runs two hours after the messaging one
        if (step, phase) == ("messaging", "committed"):
            site.operator.change(lambda state: state["offset_hours"].update(calendar=-2))
    monkeypatch.setattr(admin, "checkpoint", checkpoint)
    code, _, err = extend(site, capsys)
    assert code == 0, err
    after = expiries(site)
    assert after["calendar"] - after["messaging"] >= timedelta(hours=1)
    assert after["gateway"] == min(after[name] for name in STORES if name != "gateway")


# spec: ANYM-497
@pytest.mark.parametrize("hours", [3, -3], ids=["backend clock behind the host", "backend clock ahead of the host"])
def test_extend_never_lets_the_gateway_outlive_a_backend_whatever_the_clocks(site, capsys, hours):
    # The fake store's expiry is its now + days - offset: a positive offset is a clock behind the host's.
    site.operator.change(lambda state: state["offset_hours"].update(messaging=hours, calendar=hours))
    code, result, err = extend(site, capsys)
    assert code == 0, err
    after = expiries(site)
    assert all(after["gateway"] <= after[name] for name in STORES)
    assert after["gateway"] == min(after[name] for name in STORES if name != "gateway")
    if hours > 0:
        assert after["gateway"] == after["messaging"] < after["calls"]
    assert result["expires_at"] == entry(site.policy(), "src")["expires_at"]


# spec: ANYM-497
def test_extend_refuses_before_any_write_when_a_store_would_move_earlier(site, capsys):
    # Every store of `src` ends in 90 days (LATER): 30 days would move each one earlier.
    unchanged(site, capsys, lambda: extend(site, capsys, days="30"), "would move it earlier")


# spec: ANYM-497
@pytest.mark.parametrize("case", ["messaging revoked", "archive revoked", "calendar expired", "calls disabled",
                                  "gateway expired"])
def test_extend_refuses_before_any_write_when_a_credential_is_revoked_or_expired(site, capsys, case):
    def edit(state):
        if case == "messaging revoked":
            state["messaging"]["keys"]["key-src"]["revoked"] = True
        elif case == "archive revoked":
            state["messaging"]["keys"]["key-src-calls-archive"]["revoked"] = True
        elif case == "calendar expired":
            state["calendar"]["agents"]["src"]["expires_at"] = PAST
    site.operator.change(edit)
    if case == "calls disabled":
        site.registry[0]["enabled"] = False
        private(Path(site.config["calls_registry"]), {"version": 1, "revision": 5, "principals": site.registry})
    if case == "gateway expired":
        site.principals[0]["expires_at"] = PAST
        site.write_policy()
    unchanged(site, capsys, lambda: extend(site, capsys), "revoked or expired" if case != "calls disabled"
              else "calls credential is revoked")


# spec: ANYM-497
@pytest.mark.parametrize("days", ["0", "184"])
def test_extend_refuses_days_outside_1_to_183(site, capsys, days):
    unchanged(site, capsys, lambda: extend(site, capsys, days=days), "--days is 1-183")


# spec: ANYM-497
def test_extend_dry_run_prints_previous_and_planned_expiry_and_writes_nothing(site, capsys):
    before = site.snapshot()
    code, result, err = extend(site, capsys, "--dry-run")
    assert code == 0 and result["dry_run"] is True, err
    assert {row["store"] for row in result["stores"]} == set(STORES)
    assert all(row["previous_expires_at"] and row["planned_expires_at"] for row in result["stores"])
    assert site.snapshot() == before and site.operator.state["extended"] == []


# spec: ANYM-497
@pytest.mark.parametrize("sharing", ["direct", "nested archive"])
@pytest.mark.parametrize("verb", ["extend", "revoke"])
def test_extend_and_revoke_refuse_a_credential_another_principal_uses(site, capsys, sharing, verb):
    if sharing == "direct":
        site.principals[1]["credentials"]["calendar"] = dict(site.principals[0]["credentials"]["calendar"])
        site.write_policy()
    else:
        private(site.voice / "other-archive.json", {"base_url": "http://messaging.invalid",
                                                   "token_file": "/run/secrets/src-archive.token"})
    run = (lambda: extend(site, capsys)) if verb == "extend" else (lambda: revoke(site, capsys))
    unchanged(site, capsys, run, "also used by other")


# spec: ANYM-497
def test_undo_of_an_extend_is_refused(site, capsys):
    code, result, err = extend(site, capsys)
    assert code == 0, err
    before = site.snapshot()
    code, _, err = call(site, capsys, "undo", result["operation"])
    assert code == 2 and "expiries never move earlier" in err and site.snapshot() == before


EXTEND_STEPS = [("messaging", "committed"), ("messaging-archive", "committed"), ("calendar", "committed"),
                ("calls registry", "renamed"), ("gateway policy", "renamed")]


# spec: ANYM-497
@pytest.mark.parametrize("point", EXTEND_STEPS, ids=[step for step, _ in EXTEND_STEPS])
def test_a_failure_after_each_extend_step_is_finished_by_a_rerun(site, capsys, monkeypatch, point):
    crash_at(monkeypatch, point)
    with pytest.raises(Crash):
        site.main("key", "extend", "src", "--days", "120")
    monkeypatch.setattr(admin, "checkpoint", lambda step, phase: None)
    operation = started(site)
    code, result, err = extend(site, capsys, "--operation", operation)
    assert code == 0, err
    # A committed step is read back, never repeated blind: each backend credential moved once.
    assert sorted(site.operator.state["extended"]) == ["key-src", "key-src-calls-archive", "src"]
    after = expiries(site)
    assert after["gateway"] == min(after[name] for name in STORES if name != "gateway")
    assert site.journal(operation)["state"] == "done" and result["operation"] == operation


# key revoke

def order(monkeypatch) -> list:
    seen = []
    monkeypatch.setattr(admin, "checkpoint", lambda step, phase: seen.append(step))
    return seen


# spec: ANYM-497
def test_revoke_disables_the_gateway_first_then_every_backend_and_keeps_the_records(site, capsys, monkeypatch):
    seen = order(monkeypatch)
    code, result, err = revoke(site, capsys)
    assert code == 0, err
    assert seen[0] == "gateway policy" and seen[1:] == ["messaging", "messaging-archive", "calendar", "calls registry"]
    state = site.operator.state
    assert state["messaging"]["keys"]["key-src"]["revoked"] and state["messaging"]["keys"]["key-src-calls-archive"]["revoked"]
    assert state["calendar"]["agents"]["src"]["revoked"] is True
    assert entry(site.policy(), "src")["enabled"] is False and entry(site.registry_doc(), "src")["enabled"] is False
    assert list(site.keys.glob("src-*")) and list(site.voice.glob("src-*"))  # records and files stay for audit
    other = state["messaging"]["keys"]["key-other"]
    assert other["revoked"] is False and entry(site.policy(), "other")["enabled"] is True
    # Without --token-file: the in-container policy digest of each gateway, and the output says no bearer probe ran.
    assert result["evidence"]["gateways"] == ["deedkit-gateway-1"] and site.docker.probes == []
    assert "bearer probe was not run" in result["evidence"]["bearer_probe"]


# spec: ANYM-497
def test_revoke_with_the_matching_token_requires_a_401_after_a_control_succeeds(site, capsys):
    token = private(site.root / "mac/src.token", site.tokens["src-gateway"] + "\n")
    code, result, err = revoke(site, capsys, "--token-file", str(token))
    assert code == 0, err
    assert site.docker.probes == [(site.gateway["Id"], 2)]  # control, then the revoked bearer, on stdin only
    assert "rejected (401)" in result["evidence"]["bearer_probe"]


# spec: ANYM-497
def test_revoke_with_a_wrong_token_refuses_before_any_write(site, capsys):
    token = private(site.root / "mac/src.token", site.tokens["other-gateway"] + "\n")
    unchanged(site, capsys, lambda: revoke(site, capsys, "--token-file", str(token)), "does not match")


# spec: ANYM-497
def test_revoke_with_a_missing_token_file_refuses_before_any_write(site, capsys):
    unchanged(site, capsys, lambda: revoke(site, capsys, "--token-file", str(site.root / "mac/none.token")),
              "unreadable")


# spec: ANYM-497
def test_a_401_for_an_unrelated_reason_is_not_counted(site, capsys):
    token = private(site.root / "mac/src.token", site.tokens["src-gateway"] + "\n")
    site.docker.bearer = lambda container, value: 401  # e.g. a wrong Host: every bearer is refused
    code, _, err = revoke(site, capsys, "--token-file", str(token))
    assert code == 1 and "control" in err and "not proven" in err, err
    site.docker.bearer = site.answer
    code, result, err = revoke(site, capsys, "--token-file", str(token), "--operation", started(site))
    assert code == 0 and "rejected (401)" in result["evidence"]["bearer_probe"], err


# spec: ANYM-497
def test_revoke_reports_a_gateway_whose_policy_is_not_the_published_file(site, capsys):
    site.docker.inside[site.gateway["Id"]] = Path(site.config["policy"]).read_bytes()  # the old policy it still reads
    code, _, err = revoke(site, capsys)
    assert code == 1 and "deedkit-gateway-1" in err and "not proven" in err, err


# spec: ANYM-497
def test_revoke_dry_run_writes_nothing(site, capsys):
    before = site.snapshot()
    code, result, err = revoke(site, capsys, "--dry-run")
    assert code == 0 and result["dry_run"] is True, err
    assert result["steps"] == ["gateway policy", "messaging", "messaging-archive", "calendar", "calls registry"]
    assert site.snapshot() == before


# spec: ANYM-497
def test_undo_of_a_revoke_re_enables_only_the_gateway_and_calls_entries(site, capsys):
    code, result, err = revoke(site, capsys)
    assert code == 0, err
    code, undone, err = call(site, capsys, "undo", result["operation"])
    assert code == 0, err
    assert entry(site.policy(), "src")["enabled"] is True and entry(site.registry_doc(), "src")["enabled"] is True
    state = site.operator.state
    assert state["messaging"]["keys"]["key-src"]["revoked"] and state["calendar"]["agents"]["src"]["revoked"]
    assert "messaging and calendar revocations are final" in undone["note"]
    assert undone["enabled"] == ["gateway policy", "calls registry"]


REVOKE_STEPS = [("gateway policy", "renamed"), ("messaging", "committed"), ("messaging-archive", "committed"),
                ("calendar", "committed"), ("calls registry", "renamed")]


# spec: ANYM-497
@pytest.mark.parametrize("then", ["rerun", "undo"])
@pytest.mark.parametrize("point", REVOKE_STEPS, ids=[step for step, _ in REVOKE_STEPS])
def test_a_failure_after_each_revoke_step_is_finished_by_a_rerun_or_compensated(site, capsys, monkeypatch,
                                                                                    point, then):
    crash_at(monkeypatch, point)
    with pytest.raises(Crash):
        site.main("key", "revoke", "src")
    operation = started(site)
    assert entry(site.policy(), "src")["enabled"] is False  # the gateway step always ran first
    monkeypatch.setattr(admin, "checkpoint", lambda step, phase: None)
    if then == "rerun":
        code, _, err = revoke(site, capsys, "--operation", operation)
        assert code == 0, err
        state = site.operator.state
        assert state["messaging"]["keys"]["key-src"]["revoked"] and state["calendar"]["agents"]["src"]["revoked"]
        assert entry(site.registry_doc(), "src")["enabled"] is False
        assert site.journal(operation)["state"] == "done"
    else:
        code, undone, err = call(site, capsys, "undo", operation)
        assert code == 0, err
        assert entry(site.policy(), "src")["enabled"] is True and entry(site.registry_doc(), "src")["enabled"] is True
        assert "final" in undone["note"]


# spec: ANYM-497
def test_revoke_never_puts_a_token_or_hash_in_output_argv_or_the_journal(site, capsys):
    token = private(site.root / "mac/src.token", site.tokens["src-gateway"] + "\n")
    texts = []
    for argv in (("key", "extend", "src", "--days", "120"), ("key", "revoke", "src", "--token-file", str(token))):
        code = site.main(*argv)
        out, err = capsys.readouterr()
        assert code == 0, err
        texts += [out, err]
    texts += [json.dumps(site.docker.calls), json.dumps(site.operator.state["log"])]
    texts += [p.read_text() for p in (site.root / "ops").glob("*.json")]
    for secret in site.tokens.values():
        for needle in (secret, hashlib.sha256(secret.encode()).hexdigest()):
            assert not [text for text in texts if needle in text], secret[:20]
