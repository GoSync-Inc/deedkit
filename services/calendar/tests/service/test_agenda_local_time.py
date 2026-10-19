"""calendar_agenda states each event in the calendar's configured local zone and names its calendar (DK-39)."""

from family_calendar.service.core import FamilyService
from tests.service.test_policy import SCOPES

START, END = "2026-10-19T00:00:00Z", "2026-10-20T00:00:00Z"


def publish(household):
    household.publish_window(
        "cal-a",
        START,
        END,
        [
            {
                "resource_ref": "res-a",
                "target": "private-a",
                "etag": '"v1"',
                "payload": {},
                "occurrences": [
                    {
                        "ref": "occ-a",
                        "start": "2026-10-19T09:30:00Z",
                        "end": "2026-10-19T10:30:00Z",
                        "details": {"title": "Lesson"},
                        "busy": True,
                    }
                ],
            }
        ],
        "complete",
    )


# spec: ANYM-253
def test_agenda_events_carry_local_time_and_calendar_label(household):
    household.grant("family", "adult-a", "cal-a", SCOPES, "details")
    key = household.agent("agent", "family", "adult-a", SCOPES, ["cal-a"])
    publish(household)
    event = FamilyService(household).agenda(key, ["cal-a"], START, END)["events"][0]
    assert event["start"] == "2026-10-19T09:30:00+00:00" and event["end"] == "2026-10-19T10:30:00+00:00"
    assert event["local_start"] == "2026-10-19T17:30:00+08:00" and event["local_end"] == "2026-10-19T18:30:00+08:00"
    assert event["timezone"] == "Asia/Makassar"
    assert event["calendar_label"] == "Private" and event["title"] == "Lesson"


# spec: ANYM-253
def test_busy_only_agenda_never_shows_the_real_calendar_label(household):
    household.grant("family", "adult-a", "cal-a", SCOPES, "details")
    by_agent = household.agent("by-agent", "family", "adult-a", SCOPES, ["cal-a"], "busy_only")
    publish(household)
    event = FamilyService(household).agenda(by_agent, ["cal-a"], START, END)["events"][0]
    assert event["calendar_label"] == "Calendar" and event["local_start"] == "2026-10-19T17:30:00+08:00"
    assert "Private" not in str(event) and "title" not in event
    household.grant("family", "adult-a", "cal-a", SCOPES, "busy_only")
    by_grant = household.agent("by-grant", "family", "adult-a", SCOPES, ["cal-a"])
    event = FamilyService(household).agenda(by_grant, ["cal-a"], START, END)["events"][0]
    assert event["calendar_label"] == "Calendar" and "Private" not in str(event)


# spec: ANYM-253
def test_agenda_with_an_unknown_calendar_zone_keeps_utc_and_omits_local_fields(household):
    household.grant("family", "adult-a", "cal-a", SCOPES, "details")
    key = household.agent("agent", "family", "adult-a", SCOPES, ["cal-a"])
    publish(household)
    household.query("UPDATE calendars SET timezone='Not/AZone' WHERE ref='cal-a'")
    event = FamilyService(household).agenda(key, ["cal-a"], START, END)["events"][0]
    assert event["start"] == "2026-10-19T09:30:00+00:00" and event["calendar_label"] == "Private"
    assert not {"local_start", "local_end", "timezone"} & set(event)
