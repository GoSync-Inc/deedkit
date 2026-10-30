"""Real HTTP + PostgreSQL preservation tests, with no collector/provider process."""

import hashlib

import httpx

from messaging_service.app import create_app
from messaging_service.subscriptions import match_subscriptions
from runtime_helpers import call, gateway, port, server
from test_messenger_commands import operator as operator, operation_args
from test_subscriptions import emit


def test_existing_receipt_subscription_cursor_and_gateway_rotation(seeded, tmp_path):
    store, _, other = seeded
    scopes = ["read", "cursor-write", "subscription-write"]
    key = store.issue_key(
        "cutover-fixture", ["telegram-demo"], permissions=scopes, key_permissions=scopes
    )
    subscription = store.create_subscription(key, ["telegram-demo-chat"])
    sid = subscription["subscription_id"]
    emit(store)
    emit(store)
    match_subscriptions(store)
    first = store.get_updates(key, "cutover", limit=1)
    store.ack_updates(key, "cutover", first["receipt"], first["ack_version"])
    pending = store.get_updates(key, "cutover", limit=1)
    before = store.get_subscription_events(key, sid, limit=1)
    selected_port = port()
    base = f"http://127.0.0.1:{selected_port}"
    tools = [
        "messages_" + n
        for n in [
            "get_updates",
            "ack_updates",
            "get_subscription",
            "get_subscription_events",
        ]
    ]
    with (
        server(create_app(store, [f"127.0.0.1:{selected_port}"]), selected_port),
        gateway(tmp_path, {"messaging": (base, key)}, {"messaging": scopes}, tools) as (
            client,
            doc,
            save,
        ),
    ):
        page = call(
            client,
            "messages_get_updates",
            {"consumer": "cutover", "limit": 1, "detail": "full", "compact": False},
        )
        assert (
            page["items"] == pending["items"]
            and page["ack_version"] == pending["ack_version"]
        )
        assert call(
            client, "messages_get_subscription", {"subscription_id": sid}
        ) == store.get_subscription(key, sid)
        events = call(
            client,
            "messages_get_subscription_events",
            {"subscription_id": sid, "limit": 1},
        )
        assert events == before
        assert (
            store.get_updates(key, "cutover", limit=1)["ack_version"]
            == pending["ack_version"]
        )
        args = {
            "consumer": "cutover",
            "receipt": pending["receipt"],
            "expected_version": pending["ack_version"],
        }
        ack = call(client, "messages_ack_updates", args)
        assert ack["ack_version"] == pending["ack_version"] + 1
        assert call(client, "messages_ack_updates", args)["replayed"]
        assert store.get_updates(other, "cutover")["ack_version"] == 0
        doc["revision"] += 1
        doc["principals"][0]["key_sha256"] = hashlib.sha256(
            b"rotated-gateway"
        ).hexdigest()
        save()
        assert (
            client.post(
                "/v1/messages_get_updates", json={"consumer": "cutover"}
            ).status_code
            == 401
        )
        client.headers["Authorization"] = "Bearer rotated-gateway"
        assert (
            call(client, "messages_get_updates", {"consumer": "cutover"})["ack_version"]
            == ack["ack_version"]
        )
        after = call(
            client,
            "messages_get_subscription_events",
            {"subscription_id": sid, "after": before["next_after"]},
        )
        assert after == store.get_subscription_events(
            key, sid, after=before["next_after"]
        )
        assert not (
            {x["event_id"] for x in before["items"]}
            & {x["event_id"] for x in after["items"]}
        )
        # Switching the backend key cannot silently adopt the old subscription.
        replacement = store.issue_key(
            "cutover-fixture",
            ["telegram-demo"],
            permissions=scopes,
            key_permissions=scopes,
        )
        credential = doc["principals"][0]["credentials"]["messaging"]
        from pathlib import Path

        Path(credential["key_file"]).write_text(replacement)
        credential["key_sha256"] = hashlib.sha256(replacement.encode()).hexdigest()
        doc["revision"] += 1
        save()
        assert (
            client.post(
                "/v1/messages_get_subscription", json={"subscription_id": sid}
            ).status_code
            == 404
        )


def test_original_download_bytes_and_backend_revocation(seeded, tmp_path):
    store, key, _ = seeded
    selected_port = port()
    base = f"http://127.0.0.1:{selected_port}"
    old_url = store.public_url
    store.public_url = base
    try:
        with (
            server(create_app(store, [f"127.0.0.1:{selected_port}"]), selected_port),
            gateway(
                tmp_path,
                {"messaging": (base, key)},
                {"messaging": ["download"]},
                ["messages_get_attachment"],
            ) as (client, doc, save),
        ):
            item = call(
                client, "messages_get_attachment", {"attachment_id": "demo-file-1"}
            )
            assert item["download_url"].startswith(
                str(client.base_url).rstrip("/") + "/v1/files/"
            )
            data = client.get(item["download_url"])
            assert data.status_code == 200 and len(data.content) == item["bytes"]
            assert hashlib.sha256(data.content).hexdigest() == item["sha256"]
            direct = store.get_attachment(key, "demo-file-1")["download_url"]
            assert (
                httpx.head(direct).status_code == 200
            )  # Active stream revalidation uses this actual route.
            store.revoke(key)
            assert httpx.head(direct).status_code == 404
            assert client.get(item["download_url"]).status_code == 404
    finally:
        store.public_url = old_url


def test_action_ceiling_preserves_backend_payload_digest_and_idempotency(
    operator, tmp_path
):
    store, _, key, _ = operator
    store.set_messenger_operations("command-fixture", ["rename_group", "leave_group"])
    selected_port = port()
    base = f"http://127.0.0.1:{selected_port}"
    tools = [
        "messages_" + name
        for name in [
            "list_messenger_operations",
            "preview_messenger_operation",
            "execute_messenger_operation",
            "get_messenger_operation",
        ]
    ]
    actions = {"messenger_operations": ["rename_group"]}
    with (
        server(create_app(store, [f"127.0.0.1:{selected_port}"]), selected_port),
        gateway(
            tmp_path,
            {"messaging": (base, key)},
            {"messaging": ["groups-manage"]},
            tools,
            actions=actions,
        ) as (client, doc, save),
    ):
        args = {
            "account": "command-fixture",
            "operation": "rename_group",
            "chat_id": operation_args()["chat_id"],
            "parameters": {"title": "Synthetic group"},
        }
        preview = call(client, "messages_preview_messenger_operation", args)
        bad = {
            **args,
            "parameters": {"title": "Changed"},
            "request_id": "synthetic-rename",
            "preview_digest": preview["preview_digest"],
        }
        assert (
            client.post(
                "/v1/messages_execute_messenger_operation", json=bad
            ).status_code
            == 409
        )
        execute = {
            **args,
            "request_id": "synthetic-rename",
            "preview_digest": preview["preview_digest"],
        }
        created = call(client, "messages_execute_messenger_operation", execute)
        assert (
            call(client, "messages_execute_messenger_operation", execute)[
                "operation_id"
            ]
            == created["operation_id"]
        )
        assert (
            call(
                client,
                "messages_get_messenger_operation",
                {"operation_id": created["operation_id"]},
            )["request_id"]
            == "synthetic-rename"
        )
        with store.pool.connection() as c:
            assert (
                c.execute("SELECT count(*) AS n FROM messenger_commands").fetchone()[
                    "n"
                ]
                == 1
            )
        doc["revision"] += 1
        doc["principals"][0]["actions"] = {}
        save()
        assert (
            client.post(
                "/v1/messages_execute_messenger_operation", json=execute
            ).status_code
            == 403
        )
