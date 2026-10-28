import pytest
from family_calendar.pilot.journal import Journal
from family_calendar.errors import CalendarError


def test_intent_is_durable_after_reopen_and_payload_conflict_is_rejected(tmp_path):
    path = tmp_path / "run"
    j = Journal(path, "a", "cal1")
    intent = {
        "kind": "create",
        "href": "https://caldav.icloud.com/a/cal/test.ics",
        "desired": b"event",
        "etag": None,
    }
    step = j.prepare("stable-command", intent)
    j.close()
    reopened = Journal(path, "a", "cal1")
    assert reopened.get(step)["state"] == "prepared"
    assert reopened.prepare("stable-command", intent) == step
    with pytest.raises(CalendarError, match="idempotency_conflict"):
        reopened.prepare("stable-command", {**intent, "desired": b"different"})
    assert (path / "journal.sqlite").stat().st_mode & 0o777 == 0o600
    reopened.close()


def test_run_scope_cannot_be_changed_on_resume(tmp_path):
    j = Journal(tmp_path / "run", "a", "cal1")
    j.close()
    with pytest.raises(CalendarError, match="run_scope_mismatch"):
        Journal(tmp_path / "run", "b", "cal1")


def test_cleanup_owns_only_verified_created_objects(tmp_path):
    j = Journal(tmp_path / "run", "a", "cal1")
    s = j.prepare(
        "create-1",
        {"kind": "create", "href": "https://caldav.icloud.com/a/c/a.ics", "desired": b"a", "etag": None},
    )
    assert j.owned_objects() == []
    j.mark(s, "verified", etag='"v1"')
    assert j.owned_objects()[0]["href"].endswith("a.ics")
    assert j.owned_objects()[0]["etag"] == '"v1"'
    j.close()


def test_step_claim_is_atomic_across_two_executors(tmp_path):
    j = Journal(tmp_path / "run", "a", "cal1")
    s = j.prepare(
        "create",
        {"kind": "create", "href": "https://caldav.icloud.com/a/c/a.ics", "desired": b"a", "etag": None},
    )
    other = Journal(tmp_path / "run", "a", "cal1")
    assert j.claim(s) is True
    assert other.claim(s) is False
    other.close()
    j.close()


# spec: ANYM-365
def test_run_scope_rejects_another_calendar_on_resume(tmp_path):
    j = Journal(tmp_path / "run", "a", "cal1")
    j.close()
    with pytest.raises(CalendarError, match="run_scope_mismatch"):
        Journal(tmp_path / "run", "a", "cal2")


# spec: ANYM-365
def test_rejected_intent_is_never_stored(tmp_path):
    j = Journal(tmp_path / "run", "a", "cal1")
    href = "https://caldav.icloud.com/a/c/a.ics"
    with pytest.raises(CalendarError, match="conditional_write_unavailable"):
        j.prepare("command", {"kind": "update", "href": href, "desired": b"new", "etag": None})
    with pytest.raises(CalendarError, match="invalid_operation_kind"):
        j.prepare("command", {"kind": "invite", "href": href, "desired": b"new", "etag": '"v1"'})
    assert j.find("command") is None
    assert j.steps() == []
    # Nothing was stored, so the request ID is still free for a valid intent.
    step = j.prepare("command", {"kind": "update", "href": href, "desired": b"new", "etag": '"v1"'})
    assert [s["id"] for s in j.steps()] == [step]
    j.close()
