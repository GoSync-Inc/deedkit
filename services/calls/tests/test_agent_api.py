"""Two independent principals, persistent ownership, and provider-free admission tests."""

import hashlib
import json

import httpx
import pytest

from agent_api import AgentAPI, AgentRegistry, SCOPES
from app import create_app
from core import BriefInput, ServiceError, VoiceService
from outbound import CallingSettings, Outbound
from test_tasks import Archive, BRIEF


def registry(tmp_path):
    path = tmp_path / "agents.json"
    doc = {
        "version": 1,
        "revision": 1,
        "principals": [
            {
                "id": name,
                "key_sha256": hashlib.sha256(name.encode()).hexdigest(),
                "enabled": True,
                "expires_at": "2099-01-01T00:00:00Z",
                "scopes": sorted(SCOPES),
                "channels": ["browser", "phone"],
                "destinations": {"phone": ["+12025550123"]},
            }
            for name in ("alice", "bob")
        ],
    }

    def save():
        path.write_text(json.dumps(doc))
        path.chmod(0o600)

    save()
    return path, doc, save


def api(service, path, outbound=None, archive_factory=None):
    async def start(identity):
        return service.start(identity)

    async def stop(identity):
        service.stop(identity, "agent_stop")
        return service.get_call(identity)

    return AgentAPI(
        service, AgentRegistry(path), start, stop, outbound, archive_factory
    )


async def test_ownership_cannot_be_spoofed_and_survives_restart(tmp_path):
    path, _, _ = registry(tmp_path)
    service = VoiceService(tmp_path / "data")
    surface = api(service, path)
    body = {**BRIEF, "source_message_ids": []}
    for extra in ({"owner_principal": "bob"}, {"owner": "bob"}):
        with pytest.raises(ServiceError, match="invalid_arguments"):
            await surface.call("alice", "prepare_call", {"brief": {**body, **extra}})
    # A shared learned supplement must not enter a scoped principal's context.
    service.learning.active_candidate = lambda: {
        "prompt_version": "private",
        "instruction": "BOB SECRET",
    }
    brief = await surface.call("alice", "prepare_call", {"brief": body})
    assert brief["owner_principal"] == "alice" and "BOB SECRET" not in json.dumps(brief)
    for operation in ("start_call", "preflight_call", "dial_call"):
        arguments = {"brief_id": brief["id"]}
        if operation == "dial_call":
            arguments["authorization_reference"] = "synthetic instruction"
            arguments["destination_source"] = "agent_found"
            arguments["destination_evidence"] = "synthetic directory"
        with pytest.raises(ServiceError, match="not_found_or_forbidden"):
            await surface.call("bob", operation, arguments)
    call = await surface.call("alice", "start_call", {"brief_id": brief["id"]})
    for operation in (
        "get_call_status",
        "get_call_result",
        "get_call_analysis",
        "stop_call",
        "prepare_followup",
    ):
        arguments = {"call_id": call["id"]}
        if operation == "prepare_followup":
            arguments["decision_id"] = "synthetic"
        with pytest.raises(ServiceError, match="not_found_or_forbidden"):
            await surface.call("bob", operation, arguments)
    await surface.call("alice", "stop_call", {"call_id": call["id"]})
    legacy = service.prepare(BriefInput(**body), [])
    with pytest.raises(ServiceError, match="not_found_or_forbidden"):
        await surface.call("alice", "start_call", {"brief_id": legacy["id"]})
    service.close()
    service = VoiceService(tmp_path / "data")
    surface = api(service, path)
    assert (await surface.call("alice", "get_call_result", {"call_id": call["id"]}))[
        "id"
    ] == call["id"]
    with pytest.raises(ServiceError, match="not_found_or_forbidden"):
        await surface.call("bob", "get_call_result", {"call_id": call["id"]})
    service.close()


async def test_owner_inbox_and_followup_keep_the_original_principal(tmp_path):
    path, _, _ = registry(tmp_path)
    service = VoiceService(tmp_path / "data")
    surface = api(service, path)
    brief = await surface.call(
        "alice",
        "prepare_call",
        {
            "brief": {
                **BRIEF,
                "source_message_ids": [],
                "approval_fields": ["delivery_window"],
            }
        },
    )
    call = await surface.call("alice", "start_call", {"brief_id": brief["id"]})
    cid = call["id"]
    service.connected(cid)
    service.transcript(cid, "user", "12–13")
    decision = service.tool(
        cid,
        "record_answer",
        {"field": "delivery_window", "value": "12–13", "evidence": "12–13"},
        "time",
    )["owner_decision"]
    service.stop(cid, "awaiting_owner")
    inbox = await surface.call("alice", "get_owner_requests", {})
    assert len(inbox["items"]) == 1 and inbox["next_offset"] is None
    assert inbox["notification_channel"] == "not_connected"
    assert "only in the owner's" in inbox["notification_explanation"]
    assert (await surface.call("bob", "get_owner_requests", {}))["items"] == []
    for op in ("submit_owner_decision", "get_voice_learning", "submit_call_analysis"):
        with pytest.raises(ServiceError, match="not_found_or_forbidden"):
            await surface.call("alice", op, {})
    service.decide(cid, decision["decision_id"], "Нет")  # Trusted owner fixture only.
    child = await surface.call(
        "alice",
        "prepare_followup",
        {"call_id": cid, "decision_id": decision["decision_id"]},
    )
    assert (
        child["owner_principal"] == "alice"
        and child["followup"]["owner_answer"] == "Нет"
    )
    with pytest.raises(ServiceError, match="not_found_or_forbidden"):
        await surface.call("bob", "start_call", {"brief_id": child["id"]})
    service.close()


async def test_scope_route_revocation_and_admission_after_provider_preflight(tmp_path):
    path, doc, save = registry(tmp_path)
    service = VoiceService(tmp_path / "data")
    seen = []

    class Provider:
        async def preflight(self, brief):
            doc["revision"] += 1
            doc["principals"][0]["enabled"] = False
            save()
            return {"configured": True}

        async def dial(self, call):
            seen.append(call["id"])
            raise AssertionError("must not dial")

    outbound = Outbound(
        service,
        lambda: "synthetic",
        CallingSettings(enabled=True, allowed={"phone": ["+12025550123"]}),
        {"twilio": Provider()},
    )
    surface = api(service, path, outbound)
    body = {
        **BRIEF,
        "source_message_ids": [],
        "channel": "phone",
        "account": "twilio",
        "destination": "+12025550123",
    }
    # No destination list since ANYM-288; an ungranted channel is still refused.
    with pytest.raises(ServiceError, match="not_found_or_forbidden"):
        await surface.call(
            "alice",
            "prepare_external_call",
            {"brief": {**body, "channel": "whatsapp"}},
        )
    brief = await surface.call("alice", "prepare_external_call", {"brief": body})
    doc["principals"][0]["scopes"].remove("calls:dial")
    doc["revision"] += 1
    save()
    with pytest.raises(ServiceError, match="not_found_or_forbidden"):
        await surface.call(
            "alice",
            "dial_call",
            {
                "brief_id": brief["id"],
                "authorization_reference": "synthetic",
                "destination_source": "agent_found",
                "destination_evidence": "synthetic directory",
            },
        )
    doc["principals"][0]["scopes"].append("calls:dial")
    doc["revision"] += 1
    save()
    with pytest.raises(ServiceError, match="unauthorized"):
        await surface.call(
            "alice",
            "dial_call",
            {
                "brief_id": brief["id"],
                "authorization_reference": "synthetic",
                "destination_source": "agent_found",
                "destination_evidence": "synthetic directory",
            },
        )
    assert seen == [] and service.list_calls() == []
    service.close()


async def test_archive_is_per_principal_and_no_owner_archive_fallback(tmp_path):
    path, doc, save = registry(tmp_path)
    service = VoiceService(tmp_path / "data")
    seen = []

    class Scoped:
        async def message(self, identity):
            seen.append(identity)
            return {"id": identity, "body": "ALICE ONLY"}

        async def close(self):
            seen.append("closed")

    def factory(principal):
        assert (
            principal.id == "alice"
            and principal.archive_config == "/private/alice.json"
        )
        return Scoped()

    surface = api(service, path, archive_factory=factory)
    with pytest.raises(ServiceError, match="scoped_archive_not_configured"):
        await surface.call("bob", "prepare_call", {"brief": BRIEF})
    assert seen == []
    doc["principals"][0]["archive_config"] = "/private/alice.json"
    doc["revision"] += 1
    save()
    brief = await surface.call("alice", "prepare_call", {"brief": BRIEF})
    assert (
        seen == ["selected-message", "closed"]
        and brief["facts"][1]["text"] == "ALICE ONLY"
    )
    service.close()


async def test_http_surface_separates_owner_control_and_bounded_agent_body(tmp_path):
    path, doc, save = registry(tmp_path)
    app = create_app(
        tmp_path / "data", Archive(), control_token="owner-only", agent_registry=path
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://testserver"
        ) as c:
            assert (await c.post("/v1/capabilities", json={})).status_code == 401
            assert (
                await c.post(
                    "/v1/capabilities",
                    json={},
                    headers={"Authorization": "Bearer owner-only"},
                )
            ).status_code == 401
            headers = {"Authorization": "Bearer alice"}
            assert (
                await c.post("/control/mcp", json={}, headers=headers)
            ).status_code == 401
            assert (
                await c.post("/api/credentials", json={}, headers=headers)
            ).status_code == 401
            cap = await c.post("/v1/capabilities", json={}, headers=headers)
            assert cap.status_code == 200 and cap.json()["principal"] == "alice"
            assert (
                await c.post("/v1/prepare_call", content=b"x" * 16385, headers=headers)
            ).status_code == 413
            doc["principals"][0]["enabled"] = False
            doc["revision"] += 1
            save()
            assert (
                await c.post("/v1/capabilities", json={}, headers=headers)
            ).status_code == 401


def test_registry_fails_closed_for_rollback_symlink_and_unsafe_permissions(tmp_path):
    path, doc, save = registry(tmp_path)
    policy = AgentRegistry(path)
    assert policy.authenticate("alice").id == "alice"
    doc["revision"] += 1
    save()
    policy.authenticate("alice")
    doc["revision"] -= 1
    save()
    with pytest.raises(ServiceError, match="agent_policy_unavailable"):
        policy.authenticate("alice")
    doc["revision"] += 2
    save()
    path.chmod(0o644)
    with pytest.raises(ServiceError, match="agent_policy_unavailable"):
        policy.authenticate("alice")
    path.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(ServiceError, match="agent_policy_unavailable"):
        AgentRegistry(link).authenticate("alice")


async def test_lists_are_owned_newest_first_paged_and_summarized(tmp_path):
    path, doc, save = registry(tmp_path)
    service = VoiceService(tmp_path / "data")
    surface = api(service, path)
    body = {**BRIEF, "source_message_ids": [], "owner_notes": "SYNTHETIC PRIVATE NOTE"}
    first = await surface.call("alice", "prepare_call", {"brief": body})
    phone = {**body, "channel": "phone", "account": "twilio"}
    external = await surface.call(
        "alice",
        "prepare_external_call",
        {"brief": {**phone, "destination": "+12025550123", "goal": "g" * 300}},
    )
    await surface.call("bob", "prepare_call", {"brief": body})
    service.prepare(BriefInput(**body), [])  # Legacy brief without an owner.
    call = await surface.call("alice", "start_call", {"brief_id": first["id"]})
    page = await surface.call("alice", "list_briefs", {"limit": 1})
    assert [row["id"] for row in page["items"]] == [external["id"]]
    assert page["next_offset"] == 1 and page["items"][0]["channel"] == "phone"
    assert len(page["items"][0]["goal"]) == 120 and page["items"][0]["call_id"] is None
    last = await surface.call("alice", "list_briefs", {"limit": 1, "offset": 1})
    assert last == {
        "items": [
            {
                "id": first["id"],
                "created_at": first["created_at"],
                "channel": "browser",
                "counterparty": BRIEF["counterparty"],
                "goal": BRIEF["goal"],
                "followup_of": None,
                "call_id": call["id"],
                "call_status": "waiting_for_browser",
            }
        ],
        "next_offset": None,
    }
    calls = await surface.call("alice", "list_calls", {})
    assert calls == {
        "items": [
            {
                "id": call["id"],
                "brief_id": first["id"],
                "status": "waiting_for_browser",
                "outcome": "unresolved",
                "channel": "browser",
                "destination_source": None,
                "started_at": call["started_at"],
                "ended_at": None,
                "end_reason": None,
                "counterparty": BRIEF["counterparty"],
                "goal": BRIEF["goal"],
                "pending_decisions": 0,
            }
        ],
        "next_offset": None,
    }
    private = json.dumps([page, last, calls])
    assert "SYNTHETIC PRIVATE NOTE" not in private and "+12025550123" not in private
    assert (await surface.call("bob", "list_calls", {}))["items"] == []
    assert len((await surface.call("bob", "list_briefs", {}))["items"]) == 1
    assert (await surface.call("alice", "list_calls", {"offset": 2**70}))["items"] == []
    with pytest.raises(ServiceError, match="invalid_arguments"):
        await surface.call("alice", "list_calls", {"limit": 101})
    doc["principals"][0]["scopes"].remove("calls:read")
    doc["revision"] += 1
    save()
    for operation in ("list_briefs", "list_calls"):
        with pytest.raises(ServiceError, match="not_found_or_forbidden"):
            await surface.call("alice", operation, {})
    service.close()


async def test_capabilities_report_enforced_limits(tmp_path):
    path, _, _ = registry(tmp_path)
    service = VoiceService(tmp_path / "data")
    surface = api(service, path, None)
    caps = await surface.call("alice", "capabilities", {})
    assert caps["limits"] == {
        "active_calls_per_service": 1,
        "daily_call_units": 10,
        "premium_call_units": 3,
        "max_duration_secs": {"minimum": 30, "maximum": 600, "default": 600},
        "source_message_ids": 8,
        "success_fields": 8,
        "approval_fields": 8,
        "page_limit": 100,
        "request_body_bytes": 16384,
    }
    assert caps["live_call_verification"] == "not_claimed"
    service.close()
