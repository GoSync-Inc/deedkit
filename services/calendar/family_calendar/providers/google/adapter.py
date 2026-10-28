import inspect
from urllib.parse import quote
import httpx
from family_calendar.errors import CalendarError


class GoogleAdapter:
    def __init__(self, connection_id, client, token):
        self.connection_id, self.client, self.token = connection_id, client, token

    async def request(
        self, method, path, *, body=None, params=None, headers=None, mutation=False, missing=False
    ):
        token = self.token()
        if inspect.isawaitable(token):
            token = await token
        try:
            response = await self.client.request(
                method,
                "https://www.googleapis.com/calendar/v3/" + path,
                json=body,
                params=params,
                headers={"Authorization": "Bearer " + token, **(headers or {})},
                timeout=20,
                follow_redirects=False,
            )
        except httpx.TransportError:
            raise CalendarError("unknown_result" if mutation else "provider_unavailable") from None
        if missing and response.status_code in (404, 410):
            return None
        if response.status_code == 412:
            raise CalendarError("version_conflict")
        if response.status_code == 409:
            raise CalendarError("version_conflict")
        if response.status_code == 401:
            raise CalendarError("reauth_required")
        if response.status_code == 403:
            raise CalendarError("provider_readonly")
        if response.status_code == 429:
            raise CalendarError("rate_limited")
        if mutation and 400 <= response.status_code < 500:
            raise CalendarError("write_rejected")
        if not 200 <= response.status_code < 300:
            raise CalendarError(
                "unknown_result" if mutation and response.status_code >= 500 else "provider_unavailable"
            )
        if len(response.content) > 1024 * 1024:
            raise CalendarError("unknown_result" if mutation else "coverage_incomplete")
        if response.status_code == 204:
            return None
        try:
            return response.json()
        except ValueError:
            raise CalendarError("unknown_result" if mutation else "provider_unavailable") from None

    async def pages(self, path, params):
        items = []
        seen = set()
        for _ in range(20):
            page = await self.request("GET", path, params=params)
            items.extend(page.get("items", []))
            if len(items) > 5000:
                raise CalendarError("coverage_incomplete")
            cursor = page.get("nextPageToken")
            if not cursor:
                return items
            if cursor in seen:
                raise CalendarError("coverage_incomplete")
            seen.add(cursor)
            params = dict(params, pageToken=cursor)
        raise CalendarError("coverage_incomplete")

    async def discover(self):
        return await self.pages("users/me/calendarList", {"maxResults": 250})

    async def window(self, calendar, start, end):
        return await self.pages(
            "calendars/" + quote(calendar, safe="") + "/events",
            {
                "timeMin": start,
                "timeMax": end,
                "singleEvents": "true",
                "showDeleted": "true",
                "maxResults": 2500,
            },
        )

    async def read(self, calendar, event):
        return await self.request(
            "GET", "calendars/" + quote(calendar, safe="") + "/events/" + quote(event, safe=""), missing=True
        )

    async def create(self, calendar, event, send_updates="none"):
        # "all" emails each attendee the invitation (ANYM-555); every other write notifies nobody.
        if send_updates not in ("none", "all"):
            raise CalendarError("invalid_action")
        return await self.request(
            "POST",
            "calendars/" + quote(calendar, safe="") + "/events",
            body=event,
            params={"sendUpdates": send_updates},
            mutation=True,
        )

    async def patch(self, calendar, event, body, etag):
        if not etag or etag.startswith("W/"):
            raise CalendarError("conditional_write_unavailable")
        return await self.request(
            "PATCH",
            "calendars/" + quote(calendar, safe="") + "/events/" + quote(event, safe=""),
            body=body,
            params={"sendUpdates": "none"},
            headers={"If-Match": etag},
            mutation=True,
        )

    async def delete(self, calendar, event, etag):
        if not etag or etag.startswith("W/"):
            raise CalendarError("conditional_write_unavailable")
        return await self.request(
            "DELETE",
            "calendars/" + quote(calendar, safe="") + "/events/" + quote(event, safe=""),
            params={"sendUpdates": "none"},
            headers={"If-Match": etag},
            mutation=True,
        )
