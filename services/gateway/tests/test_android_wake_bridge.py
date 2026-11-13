"""Synthetic trusted bridge; zero native/device/network actions."""

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
from uuid import uuid4

from agent_gateway.android_wake_bridge import Route, projection, step


def source(**changes):
    return (
        dict(
            wake_id=str(uuid4()),
            task_id="original-task",
            request_id="original-request",
            order_id=None,
            owner_id="owner",
            device_id="phone",
            source_id="native",
            auth_epoch=1,
            kind="question",
            native_thread_id="thread",
            native_turn_id="turn",
            native_call_id=None,
            item_id="async-item",
            message_id="async-message",
            native_request_id=None,
            question_ids=[],
            expires_at=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            sequence=1,
        )
        | changes
    )


# spec: ANYM-997
def test_preorder_async_question_keeps_actual_ids_and_projects_no_words_or_protected_values():
    raw = source(
        questions=[{"title": "Which option?", "options": None}],
        owner_words="original words",
        secret="SYNTHETIC_PROTECTED_VALUE",
    )
    item, digest = projection(raw, Route("owner", "phone", "native", 1))
    assert item["order_id"] is None and item["native_request_id"] is None and item["question_ids"] == []
    assert item["item_id"] == "async-item" and item["message_id"] == "async-message"
    assert not {"questions", "owner_words", "secret"} & item.keys()
    assert (
        digest == hashlib.sha256(json.dumps(item, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    )


# spec: ANYM-997
def test_root_paused_missing_binding_makes_zero_calls():
    async def forbidden(*args):
        raise AssertionError("no caller context should be used while paused")

    result = asyncio.run(
        step(
            Route("owner", "phone", "native", 1),
            read_cursor=forbidden,
            read_source=forbidden,
            stage=forbidden,
            receipt=forbidden,
        )
    )
    assert result["status"] == "unavailable"


# spec: ANYM-997
def test_accepted_ingestion_lost_reply_restart_and_grok_ack_do_not_resend_or_claim_native_acceptance():
    route = Route("owner", "phone", "native", 1, enabled=True)
    raw = source(native_request_id=42, question_ids=["q1", "q2"])
    saved = {}
    calls = []

    async def cursor(device):
        return {"after": 0, "journal_id": None}

    async def feed(after, limit):
        return dict(journal_id="epoch", after=0, next_after=1, events=[raw], gap=False)

    async def receipt(ident):
        return saved.get(ident, {"transport_state": "not_found"})

    async def stage(payload):
        calls.append(payload)
        item, digest = projection(raw, route)
        saved[raw["wake_id"]] = {
            "wake_id": raw["wake_id"],
            "identity_hash": digest,
            "transport_state": "unknown",
            "native_status": "not_observed",
            "grok_receipt": "fetched",
        }
        raise TimeoutError("synthetic accepted but lost reply")

    first = asyncio.run(step(route, read_cursor=cursor, read_source=feed, stage=stage, receipt=receipt))
    second = asyncio.run(step(route, read_cursor=cursor, read_source=feed, stage=stage, receipt=receipt))
    assert first["status"] == "unknown" and second["status"] == "readback" and len(calls) == 1
    assert calls[0]["events"][0]["native_request_id"] == 42
    assert second["items"][0]["native_status"] == "not_observed"


# spec: ANYM-997
def test_original_rpc_type_and_source_gap_cannot_bypass_original_receipt():
    route = Route("owner", "phone", "native", 1, enabled=True)
    numeric = source(native_request_id=42, question_ids=["q1", "q2"])
    assert projection(numeric, route)[1] != projection(numeric | {"native_request_id": "42"}, route)[1]

    async def cursor(device):
        return {"after": 0}

    async def gap(after, limit):
        return dict(journal_id="reset", after=0, next_after=1, events=[numeric], gap=True)

    async def forbidden(*args):
        raise AssertionError("no stage on gap")

    result = asyncio.run(step(route, read_cursor=cursor, read_source=gap, stage=forbidden, receipt=forbidden))
    assert result == {"status": "held", "reason": "android_wake_source_gap"}


# spec: ANYM-997
def test_backend_refusal_is_a_visible_hold_and_never_an_ingestion_receipt():
    raw = source(native_call_id="actual-call")

    async def cursor(device):
        return {"after": 0}

    async def feed(after, limit):
        return dict(journal_id="epoch", after=0, next_after=1, events=[raw], gap=False)

    async def receipt(ident):
        return {"transport_state": "not_found"}

    async def refused(payload):
        return {"error": "android_wake_not_ready"}

    result = asyncio.run(
        step(
            Route("owner", "phone", "native", 1, enabled=True),
            read_cursor=cursor,
            read_source=feed,
            stage=refused,
            receipt=receipt,
        )
    )
    assert result == {"status": "held", "reason": "android_wake_ingestion_refused"}
