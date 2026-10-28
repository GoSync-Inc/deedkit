import json

import httpx


class MemoryDAV:
    def __init__(self):
        self.resources = {}
        self.puts = 0
        self.version = 0

    def __call__(self, req):
        key = str(req.url)
        current = self.resources.get(key)
        if req.method == "GET":
            return (
                httpx.Response(200, content=current[0], headers={"etag": current[1]})
                if current
                else httpx.Response(404)
            )
        if req.method == "PUT":
            if req.headers.get("if-none-match") == "*" and current:
                return httpx.Response(412)
            if req.headers.get("if-match") and (current is None or current[1] != req.headers["if-match"]):
                return httpx.Response(412)
            self.version += 1
            self.puts += 1
            etag = f'"v{self.version}"'
            self.resources[key] = (req.content, etag)
            return httpx.Response(201, headers={"etag": etag})
        if req.method == "DELETE":
            if current and req.headers.get("if-match") != current[1]:
                return httpx.Response(412)
            self.resources.pop(key, None)
            return httpx.Response(204)
        return httpx.Response(405)


class FakeGoogle:
    """In-memory Google Calendar events API: If-Match is honored and PATCH merges like JSON merge patch
    (a null value deletes the field). A deleted recurring instance stays as a cancelled instance."""

    def __init__(self, events):
        self.events = {e["id"]: dict(e) for e in events}
        self.requests = []
        self.version = 0
        self.drop_response = False

    def _etag(self):
        self.version += 1
        return f'"g{self.version}"'

    @staticmethod
    def _merge(target, patch):
        for key, value in patch.items():
            if value is None:
                target.pop(key, None)
            elif isinstance(value, dict) and isinstance(target.get(key), dict):
                FakeGoogle._merge(target[key], value)
            else:
                target[key] = value

    def __call__(self, req):
        from urllib.parse import unquote

        self.requests.append(req)
        event_id = unquote(req.url.raw_path.decode().partition("/events")[2].lstrip("/").split("?")[0])
        current = self.events.get(event_id)
        if req.method == "GET" and not event_id:
            # events.list by iCalUID, unexpanded: the master and its stored exceptions, never the instances
            # the series only generates (marked `_generated` in fixtures).
            uid = req.url.params.get("iCalUID")
            items = [e for e in self.events.values() if uid and e.get("iCalUID") == uid and not e.get("_generated")]
            return httpx.Response(200, json={"items": items})
        if req.method == "GET":
            return httpx.Response(200, json=current) if current else httpx.Response(404)
        if req.method == "POST":
            body = json.loads(req.content)
            if body["id"] in self.events:
                return httpx.Response(409)
            # Google gives every new event an iCalUID of its own.
            self.events[body["id"]] = dict(body, etag=self._etag(), iCalUID=body.get("iCalUID", body["id"] + "@google.com"))
            return httpx.Response(200, json=self.events[body["id"]])
        if current is None:
            return httpx.Response(404)
        if req.headers.get("if-match") != current["etag"]:
            return httpx.Response(412)
        if req.method == "PATCH":
            self._merge(current, json.loads(req.content))
            current["etag"] = self._etag()
        elif req.method == "DELETE":
            if current.get("recurringEventId"):
                current.update(status="cancelled", etag=self._etag())
            else:
                self.events.pop(event_id)
        else:
            return httpx.Response(405)
        if self.drop_response:
            return httpx.Response(503)
        return httpx.Response(204) if req.method == "DELETE" else httpx.Response(200, json=current)
