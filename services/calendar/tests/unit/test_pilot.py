import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import json
from family_calendar.errors import CalendarError
from family_calendar.private_files import write_private

from family_calendar.pilot.icloud import Pilot
from family_calendar.pilot.journal import Journal
from family_calendar.providers.caldav.adapter import CalDAVAdapter
from family_calendar.providers.caldav.transport import DAVTransport
from family_calendar.providers.caldav.codec import create_event
from family_calendar.operations.executor import Executor


from tests.fakes import MemoryDAV


def test_complete_mock_matrix_resume_and_owned_cleanup(tmp_path):
    server = MemoryDAV()

    async def exercise():
        t = DAVTransport("a", "secret", client=httpx.AsyncClient(transport=httpx.MockTransport(server)))
        j = Journal(tmp_path / "run", "a", "cal1")
        try:
            pilot = Pilot(CalDAVAdapter("a", t), j, "https://caldav.icloud.com/a/c/", environment="mock")
            report = await pilot.run()
            assert report["environment"] == "mock"
            assert report["gate_a"] == "not_accepted"
            assert report["checks"]["conditional_write"]["status"] == "server_verified"
            assert report["checks"]["future_split_count"]["status"] == "server_verified"
            assert report["client_visual_check"] == "pending"
            n = server.puts
            await pilot.run()
            assert server.puts == n
            cleanup = await pilot.cleanup()
            assert cleanup["remaining"] == 0
            assert server.resources == {}
            with pytest.raises(CalendarError, match="run_already_cleaned"):
                await pilot.run()
        finally:
            j.close()
            await t.close()

    asyncio.run(exercise())


def test_cleanup_refuses_object_changed_outside_journal(tmp_path):
    server = MemoryDAV()

    async def exercise():
        t = DAVTransport("a", "secret", client=httpx.AsyncClient(transport=httpx.MockTransport(server)))
        j = Journal(tmp_path / "run", "a", "cal1")
        try:
            adapter = CalDAVAdapter("a", t)
            pilot = Pilot(adapter, j, "https://caldav.icloud.com/a/c/", environment="mock")
            now = datetime(2026, 10, 19, 16, tzinfo=timezone.utc)
            data = create_event("owned@family.invalid", now, now + timedelta(hours=1), "Synthetic")
            step = j.prepare(
                "create-one",
                {
                    "kind": "create",
                    "href": "https://caldav.icloud.com/a/c/e.ics",
                    "desired": data,
                    "etag": None,
                },
            )
            await Executor(adapter, j, pilot.collection).execute(step)
            server.resources["https://caldav.icloud.com/a/c/e.ics"] = (data, '"manual-edit"')
            result = await pilot.cleanup()
            assert result["remaining"] == 1
            assert result["conflicts"] == 1
        finally:
            j.close()
            await t.close()

    asyncio.run(exercise())


def test_failed_old_series_trim_compensates_only_unchanged_new_part(tmp_path):
    server = MemoryDAV()
    original = server.__call__

    def handler(req):
        if req.method == "PUT" and req.headers.get("if-match"):
            return httpx.Response(412)
        return original(req)

    async def exercise():
        t = DAVTransport("a", "secret", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        j = Journal(tmp_path / "run", "a", "cal1")
        try:
            pilot = Pilot(CalDAVAdapter("a", t), j, "https://caldav.icloud.com/a/c/", environment="mock")
            try:
                await pilot._future("future-fault")
            except Exception:
                pass
            assert pilot._href("future-fault") in server.resources
            assert pilot._href("future-fault-next") not in server.resources
            assert any(
                s["request_id"].endswith(":compensate-new") and s["state"] == "verified" for s in j.steps()
            )
        finally:
            j.close()
            await t.close()

    asyncio.run(exercise())


def test_unknown_accepted_trim_is_reconciled_and_never_compensated(tmp_path):
    server = MemoryDAV()
    fail_once = True

    def handler(req):
        nonlocal fail_once
        response = server(req)
        if req.method == "PUT" and req.headers.get("if-match") and fail_once:
            fail_once = False
            raise httpx.ReadTimeout("timeout after accepted trim", request=req)
        return response

    async def exercise():
        t = DAVTransport("a", "secret", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        j = Journal(tmp_path / "run", "a", "cal1")
        try:
            pilot = Pilot(CalDAVAdapter("a", t), j, "https://caldav.icloud.com/a/c/", environment="mock")
            with pytest.raises(CalendarError, match="write_unknown"):
                await pilot._future("future-fault")
            assert (j.root / "future-fault-plan.json").exists()
            assert pilot._href("future-fault-next") in server.resources
            puts = server.puts
            await pilot._future("future-fault")
            assert server.puts == puts
            assert j.find("future-fault:trim-past")["state"] == "verified"
            assert not any(s["request_id"].endswith(":compensate-new") for s in j.steps())
        finally:
            j.close()
            await t.close()

    asyncio.run(exercise())


def test_revoked_password_stops_following_mutations(tmp_path):
    puts = []

    def handler(req):
        if req.method == "PUT":
            puts.append(str(req.url))
        return httpx.Response(401)

    async def exercise():
        t = DAVTransport("a", "secret", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        j = Journal(tmp_path / "run", "a", "cal1")
        try:
            report = await Pilot(
                CalDAVAdapter("a", t), j, "https://caldav.icloud.com/a/c/", environment="mock"
            ).run()
            assert len(puts) == 1
            assert report["gate_a"] == "not_accepted"
            assert report["checks"]["all_day"]["error_code"] == "blocked_after_access_error"
        finally:
            j.close()
            await t.close()

    asyncio.run(exercise())


def test_core_future_native_visual_acceptance_are_independent(tmp_path):
    async def exercise():
        t = DAVTransport("a", "secret", client=httpx.AsyncClient(transport=httpx.MockTransport(MemoryDAV())))
        j = Journal(tmp_path / "run", "a", "cal1")
        try:
            # Unit test of gate decisions, not evidence of a real iCloud connection.
            pilot = Pilot(
                CalDAVAdapter("a", t), j, "https://caldav.icloud.com/a/c/", environment="icloud_live"
            )
            await pilot.run()
            write_private(
                j.root / "visual-check.json",
                json.dumps(
                    {
                        "result": "pass",
                        "future_result": "not-checked",
                        "native_range_result": "fail",
                        "source": "owner_reported",
                    }
                ).encode(),
            )
            report = await pilot.run()
            assert report["gate_a"] == "accepted_core"
            assert report["capabilities"]["native_range"]["status"] != "verified"
            write_private(
                j.root / "visual-check.json",
                json.dumps(
                    {
                        "result": "pass",
                        "future_result": "pass",
                        "native_range_result": "fail",
                        "source": "owner_reported",
                    }
                ).encode(),
            )
            report = await pilot.run()
            assert report["gate_a"] == "accepted_core_and_future"
            assert report["capabilities"]["native_range"]["status"] != "verified"
        finally:
            j.close()
            await t.close()

    asyncio.run(exercise())


# spec: ANYM-365
def test_mock_matrix_stale_etag_update_gets_412(tmp_path):
    server = MemoryDAV()
    answers = []

    def handler(req):
        response = server(req)
        answers.append((req.method, str(req.url), req.headers.get("if-match"), response.status_code))
        return response

    async def exercise():
        t = DAVTransport("a", "secret", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        j = Journal(tmp_path / "run", "a", "cal1")
        try:
            pilot = Pilot(CalDAVAdapter("a", t), j, "https://caldav.icloud.com/a/c/", environment="mock")
            report = await pilot.run()
            assert report["checks"]["conditional_write"]["status"] == "server_verified"
            stale = j.find("conditional:stale-version")
            assert (stale["state"], stale["error_code"]) == ("conflict", "version_conflict")
            href, etag = stale["intent"]["href"], stale["intent"]["etag"]
            # The new-version update and the stale update carry the same If-Match: the first wins, the second gets 412.
            assert [a[3] for a in answers if a[:3] == ("PUT", href, etag)] == [201, 412]
            assert server.resources[href][0] == j.find("conditional:new-version")["intent"]["desired"]
        finally:
            j.close()
            await t.close()

    asyncio.run(exercise())


# spec: ANYM-365
@pytest.mark.parametrize("change", ["etag", "data"])
def test_failed_old_series_trim_keeps_changed_new_part(tmp_path, change):
    server = MemoryDAV()
    original = server.__call__
    pilot = None

    def handler(req):
        if req.method == "PUT" and req.headers.get("if-match"):
            # Someone edits the new future part before the trim of the old series is refused.
            href = pilot._href("future-fault-next")
            data, etag = server.resources[href]
            server.resources[href] = (
                (data, '"edited"') if change == "etag" else (data.replace(b"[Family Pilot]", b"[Edited]"), etag)
            )
            return httpx.Response(412)
        return original(req)

    async def exercise():
        nonlocal pilot
        t = DAVTransport("a", "secret", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        j = Journal(tmp_path / "run", "a", "cal1")
        try:
            pilot = Pilot(CalDAVAdapter("a", t), j, "https://caldav.icloud.com/a/c/", environment="mock")
            with pytest.raises(CalendarError, match="version_conflict"):
                await pilot._future("future-fault")
            assert j.find("future-fault:trim-past")["state"] == "conflict"
            assert pilot._href("future-fault") in server.resources
            assert pilot._href("future-fault-next") in server.resources
            assert j.find("future-fault:compensate-new") is None
        finally:
            j.close()
            await t.close()

    asyncio.run(exercise())
