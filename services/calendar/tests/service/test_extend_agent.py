import time

import psycopg
import pytest

from family_calendar.errors import CalendarError

SCOPES = ["agenda:read", "plan:create", "plan:execute", "event:create"]
CREATE = {
    "action": "create",
    "calendar_ref": "cal-a",
    "event": {
        "title": "Test",
        "start": "2026-10-19T08:00:00Z",
        "end": "2026-10-19T09:00:00Z",
        "timezone": "Asia/Makassar",
    },
}


def extend(store, tmp_path, principal_id, days):
    from family_calendar.service import cli

    argv = ["extend-agent", "--id", principal_id, "--days", days]
    return cli.dispatch(cli.parser().parse_args(argv), tmp_path, tmp_path, {}, store)


def principal(store, principal_id="agent"):
    return store.one(
        "SELECT p.*,array(SELECT row(a.calendar_ref,a.visibility)::text FROM agent_calendars a "
        "WHERE a.principal_id=p.id ORDER BY 1) AS calendars FROM principals p WHERE p.id=%s",
        (principal_id,),
    )


def audit(store):
    return store.query("SELECT principal_id,action,ref,outcome FROM audit ORDER BY id")


def agent(store):
    store.grant("family", "adult-a", "cal-a", SCOPES, "details")
    store.write_opt_in("cal-a", True)
    return store.agent("agent", "family", "adult-a", SCOPES, ["cal-a"])


# spec: ANYM-423
def test_extend_agent_sets_expiry_from_database_now_and_keeps_the_principal(household, tmp_path):
    from family_calendar.service.core import FamilyService

    key = agent(household)
    service = FamilyService(household)
    plan = service.plan(key, [CREATE])
    household.query("UPDATE principals SET expires_at=now()+interval '1 day' WHERE id='agent'")
    before = principal(household)
    result = extend(household, tmp_path, "agent", "45")
    after = principal(household)
    assert set(result) == {"principal_id", "previous_expires_at", "expires_at"}
    assert result["principal_id"] == "agent"
    assert result["previous_expires_at"] == before["expires_at"].isoformat()
    assert result["expires_at"] == after["expires_at"].isoformat()
    # The stored instant is the database's own now() plus N days, the clock authentication reads.
    row = household.one(
        "SELECT expires_at-now() AS left FROM principals WHERE id='agent'",
    )
    assert 45 * 86400 - 60 < row["left"].total_seconds() <= 45 * 86400
    for field in ("key_hash", "scopes", "enabled", "version", "group_id", "person_id", "calendars"):
        assert after[field] == before[field], field
    # The scope hash covers the principal version, so the earlier plan still applies.
    assert service.apply(key, plan["plan_id"], "request-after-extend")["state"] == "queued"


# spec: ANYM-423
def test_extend_agent_refuses_a_lifetime_outside_1_to_183_days(household, tmp_path):
    agent(household)
    before = principal(household)
    # The ceiling is 365 days since ANYM-499 (owner 2026-10-24); test_extend_agent_accepts_365_days_and_refuses_366.
    for days in ("0", "-1", "1.5", "abc", "", "٣", "1000", "0183"):
        with pytest.raises(CalendarError, match="^invalid_agent_lifetime$"):
            extend(household, tmp_path, "agent", days)
    assert principal(household) == before
    assert audit(household) == []
    for days in ("1", "90", "183"):
        household.query("UPDATE principals SET expires_at=now()+interval '1 hour' WHERE id='agent'")
        extend(household, tmp_path, "agent", days)


# spec: ANYM-423
def test_extended_key_authenticates_after_the_old_expiry_until_the_new_one(household, tmp_path):
    from family_calendar.service.core import FamilyService

    key = agent(household)
    service = FamilyService(household)
    household.query("UPDATE principals SET expires_at=now()+interval '2 seconds' WHERE id='agent'")
    old = principal(household)["expires_at"]
    extend(household, tmp_path, "agent", "1")
    time.sleep(3)
    assert household.one("SELECT now()>%s AS past", (old,))["past"]
    assert service.principal(key)["id"] == "agent"
    # Move the new instant a few seconds ahead, as for the old one; from it the key stops.
    [new] = household.query(
        "UPDATE principals SET expires_at=now()+interval '2 seconds' WHERE id='agent' RETURNING expires_at"
    )
    assert service.principal(key)["id"] == "agent"
    time.sleep(3)
    assert household.one("SELECT now()>%s AS past", (new["expires_at"],))["past"]
    with pytest.raises(CalendarError, match="^not_found_or_forbidden$"):
        service.principal(key)


# spec: ANYM-423
def test_extend_agent_refuses_unknown_revoked_expired_and_shortening(household, tmp_path):
    agent(household)
    household.agent("revoked", "family", "adult-a", SCOPES, ["cal-a"])
    household.revoke("revoked")
    household.agent("expired", "family", "adult-a", SCOPES, ["cal-a"])
    household.query("UPDATE principals SET expires_at=now()-interval '1 second' WHERE id='expired'")
    rows = {p: principal(household, p) for p in ("agent", "revoked", "expired")}
    for principal_id, error in (
        ("missing", "not_found_or_forbidden"),
        ("revoked", "not_found_or_forbidden"),
        ("expired", "agent_expired"),
        # A fresh key has 90 days left; 30 from now would move its expiry earlier.
        ("agent", "agent_lifetime_would_shorten"),
    ):
        with pytest.raises(CalendarError, match="^" + error + "$"):
            extend(household, tmp_path, principal_id, "30")
    assert {p: principal(household, p) for p in rows} == rows
    assert principal(household, "missing") is None
    assert audit(household) == []


# spec: ANYM-423
def test_extend_agent_writes_one_audit_row_in_the_same_transaction(household, tmp_path):
    key = agent(household)
    household.query("UPDATE principals SET expires_at=now()+interval '1 day' WHERE id='agent'")
    extend(household, tmp_path, "agent", "10")
    rows = audit(household)
    assert rows == [{"principal_id": "agent", "action": "extend_agent", "ref": None, "outcome": "extended"}]
    stored = principal(household)["key_hash"]
    assert not any(key in str(v) or stored in str(v) for row in rows for v in row.values())
    # An audit write that fails rolls the new expiry back.
    before = principal(household)
    household.query(
        "ALTER TABLE audit ADD CONSTRAINT synthetic_audit_failure CHECK (action<>'extend_agent') NOT VALID"
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        extend(household, tmp_path, "agent", "20")
    assert principal(household) == before


# spec: ANYM-499
def test_extend_agent_accepts_365_days_and_refuses_366(household, tmp_path):
    agent(household)
    before = principal(household)
    for days in ("366", "999", "0365"):
        with pytest.raises(CalendarError, match="^invalid_agent_lifetime$"):
            extend(household, tmp_path, "agent", days)
    assert principal(household) == before
    assert audit(household) == []
    for days in ("184", "365"):
        household.query("UPDATE principals SET expires_at=now()+interval '1 hour' WHERE id='agent'")
        extend(household, tmp_path, "agent", days)
        row = household.one("SELECT expires_at-now() AS left FROM principals WHERE id='agent'")
        assert int(days) * 86400 - 60 < row["left"].total_seconds() <= int(days) * 86400
