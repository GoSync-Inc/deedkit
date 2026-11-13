"""Bounded authenticated metadata bridge; A injects existing Clients/policy closures. No daemon or device API."""

from dataclasses import dataclass
import hashlib
import json


FIELDS = frozenset(
    (
        "wake_id",
        "task_id",
        "request_id",
        "order_id",
        "owner_id",
        "device_id",
        "source_id",
        "auth_epoch",
        "kind",
        "native_thread_id",
        "native_turn_id",
        "native_call_id",
        "item_id",
        "message_id",
        "native_request_id",
        "question_ids",
        "expires_at",
    )
)


@dataclass(frozen=True)
class Route:
    owner_id: str
    device_id: str
    source_id: str
    auth_epoch: int
    enabled: bool = False


def projection(raw, route):
    """Only known metadata crosses to server. Full original questions/words stay on authorized mini fetch."""
    if not isinstance(raw, dict) or not FIELDS <= raw.keys():
        raise ValueError("invalid_android_wake")
    item = {key: raw[key] for key in FIELDS}
    if any(item[key] != getattr(route, key) for key in ("owner_id", "device_id", "source_id", "auth_epoch")):
        raise ValueError("android_wake_binding_changed")
    # JSON types and array order, especially RPC42 versus "42", are deliberately unchanged.
    text = json.dumps(item, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(text.encode()) > 4096:
        raise ValueError("invalid_android_wake")
    return item, hashlib.sha256(text.encode()).hexdigest()


async def step(route, *, read_cursor, read_source, stage, receipt):
    """One bounded step. Callbacks are authenticated/owner-scoped server context, never caller-selected URLs/keys.

    Ingestion is a DB-only idempotent transaction; unknown HTTP ingestion reconciles original wake receipts.
    External transport and native callbacks are NEVER retried by this helper.
    """
    if not route.enabled:
        return {"status": "unavailable", "reason": "android_wake_paused"}
    cursor = await read_cursor(route.device_id)
    if cursor.get("status") == "unavailable":
        return cursor
    page = await read_source(cursor.get("after", 0), 4)
    if (
        not isinstance(page, dict)
        or set(page) != {"journal_id", "after", "next_after", "events", "gap"}
        or page["gap"] is not False
        or page["after"] != cursor.get("after", 0)
        or not isinstance(page["events"], list)
        or len(page["events"]) > 4
    ):
        return {"status": "held", "reason": "android_wake_source_gap"}
    items = []
    known = []
    for raw in page["events"]:
        item, digest = projection(raw, route)
        old = await receipt(item["wake_id"])
        if old.get("transport_state") != "not_found":
            if old.get("identity_hash") != digest:
                return {"status": "held", "reason": "android_wake_identity_conflict"}
            known.append(old)
            continue
        items.append(item | {"sequence": raw["sequence"]})
    if not items:
        return {"status": "readback", "items": known}
    # The server requires the complete original page for atomic cursor movement; known identities are idempotent.
    projected = [projection(raw, route)[0] | {"sequence": raw["sequence"]} for raw in page["events"]]
    payload = {
        "device_id": route.device_id,
        "journal_id": page["journal_id"],
        "after": page["after"],
        "next_after": page["next_after"],
        "events": projected,
    }
    try:
        result = await stage(payload)
    except (TimeoutError, OSError):
        evidence = [await receipt(raw["wake_id"]) for raw in projected]
        return {"status": "unknown", "items": evidence, "reason": "ingestion_reply_unknown"}
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("items"), list)
        or result.get("after") != page["next_after"]
    ):
        return {"status": "held", "reason": "android_wake_ingestion_refused"}
    return {"status": "staged", "receipt": result}
