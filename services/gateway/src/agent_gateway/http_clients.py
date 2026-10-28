"""Independent bounded pools; no provider libraries and no automatic retries."""

import asyncio
from collections import deque
from contextlib import asynccontextmanager
import hashlib
import json
import time
from urllib.parse import urlsplit

import httpx

from .catalog import CATALOG
from .policy import Denied
from .diagnostics import CONSUMPTION_OPERATIONS, consumption_failure, failure_details


def takes_wait(tool):
    return "wait_s" in tool.get("inputSchema", {}).get("properties", {})


# ANYM-544: every catalog tool that takes wait_s, and every other long wait, is admitted through a bounded wait pool,
# never the metadata pool. An entry names the pool and when the arguments make the call wait; wait_s=0 stays a plain
# call. A catalog tool with wait_s but no entry still waits in "waits" and fails test_wait_admission.
WAIT_ADMISSION = {
    # ANYM-532: a phone recipient also waits, for its open_chat_by_phone lookup.
    ("messaging", "send_message"): ("waits", lambda a: (a.get("wait_s") or 0) > 0 or bool(a.get("phone"))),
    ("messaging", "preview_message"): ("waits", lambda a: bool(a.get("phone"))),
    ("messaging", "wait_for_subscription_events"): ("waits", lambda a: True),
    # ANYM-478: a live search waits up to 20 s in its own pool.
    ("messaging", "search"): ("searches", lambda a: a.get("live", "off") != "off"),
    # ANYM-519: register and test wait up to 10 s for the signed test delivery's result.
    ("messaging", "register_delivery_endpoint"): ("waits", lambda a: True),
    ("messaging", "test_delivery_endpoint"): ("waits", lambda a: True),
    # ANYM-549: execute waits up to wait_s for its receipt. The risk-class tools reach the backend as preview or
    # execute (dispatch), so their own entries only describe when they wait: never on dry_run.
    ("messaging", "execute_messenger_operation"): ("waits", lambda a: (a.get("wait_s") or 0) > 0),
    **{("messaging", tool): ("waits", lambda a: (a.get("wait_s") or 0) > 0 and not a.get("dry_run"))
       for tool in ("change_message", "manage_chat", "change_contacts", "read_contacts")},
    # ANYM-552: a live schedule waits 10 s by default (wait_s omitted), up to 25; a dry run never waits.
    ("calendar", "schedule"): ("waits", lambda a: not a.get("dry_run") and (a.get("wait_s") is None or a["wait_s"] > 0)),
}


def wait_tools(catalog):
    return {(t["module"], t["operation"]) for t in catalog.values() if takes_wait(t)} | set(WAIT_ADMISSION)


def admission(tools, module, operation, arguments):
    """The pool of one call: uploads, searches, waits or metadata."""
    if module == "messaging" and operation == "upload_file_chunk":
        return "uploads"
    if (module, operation) not in tools:
        return "metadata"
    pool, waits = WAIT_ADMISSION.get((module, operation), ("waits", lambda a: (a.get("wait_s") or 0) > 0))
    return pool if waits(arguments) else "metadata"


# ANYM-518: subscription waits and waiting sends share this pool; capabilities show its size.
WAIT_POOL = {"active": 2, "waiting": 4}
WAIT_RETRY_AFTER_S = 5


class Budget:
    def __init__(self, concurrency=8, waiting=24):
        self.semaphore = asyncio.Semaphore(concurrency)
        self.capacity, self.pending = concurrency + waiting, 0

    @asynccontextmanager
    async def slot(self):
        if self.pending >= self.capacity:
            raise Denied("backend_busy", 503)
        self.pending += 1
        acquired = False
        try:
            try:
                await asyncio.wait_for(self.semaphore.acquire(), timeout=0.25)
                acquired = True
            except TimeoutError:
                raise Denied("backend_busy", 503) from None
            yield
        finally:
            if acquired:
                self.semaphore.release()
            self.pending -= 1


class UploadRate:
    """Bound both byte-bearing request rate and retained credential state."""

    def __init__(self):
        self.observations = {}

    def admit(self, key):
        now = time.monotonic()
        digest = hashlib.sha256(key.encode()).digest()
        if digest not in self.observations:
            if len(self.observations) >= 1024:
                self.observations = {
                    k: times for k, times in self.observations.items() if times and times[-1] > now - 1
                }
                if len(self.observations) >= 1024:
                    raise Denied("backend_busy", 503)
            self.observations[digest] = deque()
        times = self.observations[digest]
        while times and times[0] <= now - 1:
            times.popleft()
        if len(times) >= 8:
            raise Denied("upload_rate_limited", 429)
        times.append(now)


class Clients:
    def __init__(self, deployment, transports=None, *, messaging_url=None):
        self.deployment, self.transports = deployment, transports or {}
        self.messaging_url = messaging_url
        self.metadata, self.bulk, self.budgets, self.bulk_budgets = {}, {}, {}, {}
        self.uploads, self.upload_budgets = {}, {}
        self.waits, self.wait_budgets = {}, {}
        self.searches, self.search_budgets = {}, {}
        self.upload_rate = UploadRate()
        self.wait_tools = wait_tools(CATALOG)
        self.closed = False

    async def __aenter__(self):
        try:
            for module in self.deployment["modules"]:
                target = self.deployment["backends"][module]
                if module == "messaging" and self.messaging_url:
                    # Dial only the paired HTTP generation. Keep the source
                    # policy snapshot intact for dispatch and ticket guards.
                    target = {**target, "url": self.messaging_url,
                              "host_header": target.get("host_header") or urlsplit(target["url"]).netloc}
                pools = [(self.metadata, 8), (self.bulk, 2)]
                if module == "messaging":
                    pools.extend([(self.uploads, 2), (self.searches, 4)])
                if any(m == module for m, _ in self.wait_tools):
                    pools.append((self.waits, 2))
                for container, maximum in pools:
                    container[module] = httpx.AsyncClient(
                        base_url=target["url"].rstrip("/"),
                        timeout=httpx.Timeout(35, connect=5, pool=0.25),
                        trust_env=False,
                        follow_redirects=False,
                        limits=httpx.Limits(max_connections=maximum, max_keepalive_connections=maximum),
                        transport=self.transports.get(module),
                        headers={"Host": target["host_header"]} if target.get("host_header") else None,
                    )
                self.budgets[module], self.bulk_budgets[module] = Budget(), Budget(2, 4)
                if module in self.waits:
                    self.wait_budgets[module] = Budget(WAIT_POOL["active"], WAIT_POOL["waiting"])
                if module == "messaging":
                    self.upload_budgets[module] = Budget(2, 4)
                    # ANYM-478: a search with live waits up to 20 s; its own pool keeps sends and reads admitted.
                    self.search_budgets[module] = Budget(4, 4)
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, *args):
        for client in [*self.metadata.values(), *self.bulk.values(), *self.uploads.values(), *self.waits.values(), *self.searches.values()]:
            await client.aclose()
        self.upload_rate.observations.clear()
        self.closed = True

    async def call(self, module, operation, key, arguments):
        if module not in self.metadata:
            raise Denied("backend_unavailable", 503)
        pool = admission(self.wait_tools, module, operation, arguments)
        uploading = pool == "uploads"
        if uploading:
            self.upload_rate.admit(key)
        budget, client = {
            "uploads": (self.upload_budgets, self.uploads), "searches": (self.search_budgets, self.searches),
            "waits": (self.wait_budgets, self.waits), "metadata": (self.budgets, self.metadata),
        }[pool]
        budget, client = budget[module], client[module]
        async with budget.slot():
            try:
                async with (
                    asyncio.timeout(10 if uploading else 35),
                    client.stream(
                        "POST", "/v1/" + operation, headers={"Authorization": "Bearer " + key}, json=arguments
                    ) as response,
                ):
                    failed = response.status_code >= 500 or response.status_code in (301, 302, 303, 307, 308)
                    body = bytearray()
                    maximum = 65536 if failed else 8 * 1024 * 1024
                    async for chunk in response.aiter_bytes(65536):
                        if len(body) + len(chunk) > maximum:
                            if failed:
                                body.clear()
                                break
                            raise Denied("backend_response_too_large", 503)
                        body.extend(chunk)
                    if failed:
                        try:
                            failure = json.loads(body)
                        except (ValueError, UnicodeDecodeError, RecursionError):
                            failure = None
                        error = Denied("backend_unavailable", 503)
                        error.detail = failure_details(response.status_code, failure, response.headers.get("x-request-id"))
                        raise error
                    result = json.loads(body)
                    if not isinstance(result, dict):
                        raise Denied("backend_unavailable", 503)
                    if response.status_code >= 400:
                        error = Denied("backend_denied", response.status_code)
                        # Existing operation receipts/ACK errors remain visible, but
                        # never leak transport exceptions or arbitrary response text.
                        error.detail = (
                            consumption_failure(response.status_code, result, response.headers.get("x-request-id"), operation)
                            if module == "messaging" and operation in CONSUMPTION_OPERATIONS else result
                        )
                        raise error
                    return result
            except (httpx.HTTPError, ValueError, TimeoutError) as failure:
                error = Denied("backend_unavailable", 503)
                error.detail = {"error": "backend_unavailable", "reason":
                    "gateway_timeout" if isinstance(failure, (TimeoutError, httpx.TimeoutException))
                    else "invalid_backend_response" if isinstance(failure, ValueError)
                    else "backend_connection_failed"}
                raise error from None
