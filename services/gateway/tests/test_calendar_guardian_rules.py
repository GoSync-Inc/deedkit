import asyncio
import json

import pytest

from agent_gateway.dispatch import Dispatch
from agent_gateway.policy import Policy
from test_policy import config

RULE = {"rule_id": "rule-synthetic", "activity": "Dance", "title": "SYNTHETIC-PRIVATE-RULE"}


def gateway(tmp_path, local, backend, readiness=None, tools=("calendar_capabilities", "calendar_members"),
            plan=None):
    key = tmp_path / "backend.key"
    key.write_text("backend-calendar-a")
    key.chmod(0o600)
    document = config()
    document["profiles"]["reader"]["tools"] = list(tools)
    document["principals"][0]["grants"]["calendar"] = local
    document["principals"][0]["credentials"]["calendar"]["key_file"] = str(key)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(document))
    path.chmod(0o600)

    class Clients:
        deployment = Policy(path).load()["deployment"]

        async def call(self, module, operation, credential, arguments):
            if operation == "capabilities":
                caps = {"permissions": backend, "calendars": [], "features": {"agenda": True},
                        "constraints": {"plan_action_limit": 8, "plan_step_limit": 16}}
                return caps | ({"guardian_projections": readiness} if readiness else {})
            if operation == "plan":
                return plan or {"plan_id": "plan-synthetic", "steps": [], "projections_skipped": []}
            # The backend already applied its own rule-read policy for its adult key.
            return {"group_id": "family", "members": [
                {"person_id": "adult-a", "role": "group_admin", "label": "A"},
                {"person_id": "child", "role": "dependent", "label": "Child", "guardian_rules": [RULE]},
            ]}

    return Dispatch(Policy(path), Clients())


# spec: ANYM-432
@pytest.mark.parametrize(
    ("local", "backend", "shown"),
    [
        (["agenda:read", "availability:read"], ["agenda:read", "availability:read", "assignment:read"], False),
        (["agenda:read", "assignment:read"], ["agenda:read"], False),
        (["agenda:read", "assignment:read"], ["agenda:read", "assignment:read"], True),
    ],
)
def test_members_drop_guardian_rules_without_gateway_assignment_read(tmp_path, local, backend, shown):
    result = asyncio.run(gateway(tmp_path, local, backend).call("gateway-a", "calendar_members", {}))
    child = result["members"][1]
    assert ("guardian_rules" in child) is shown
    assert ("SYNTHETIC-PRIVATE-RULE" in json.dumps(result)) is shown
    assert result["members"][0] == {"person_id": "adult-a", "role": "group_admin", "label": "A"}


# spec: ANYM-432
@pytest.mark.parametrize(
    "readiness",
    [
        {"available": False, "reason": "schema_not_ready"},
        {"available": False, "reason": "grants_not_ready"},
        {"available": True},
    ],
)
def test_capabilities_keep_guardian_projection_readiness(tmp_path, readiness):
    scopes = ["agenda:read", "availability:read"]
    result = asyncio.run(gateway(tmp_path, scopes, scopes, readiness).call("gateway-a", "calendar_capabilities", {}))
    assert result["guardian_projections"] == readiness
    assert "features" not in result


PROJECT = {"action": "project", "calendar_ref": "cal-child", "event_ref": "event-lesson", "activity": "Dance"}
PLANNER = ("calendar_capabilities", "calendar_plan", "calendar_apply")
WRITER = ["agenda:read", "plan:create", "plan:execute", "event:create"]


# spec: ANYM-433
@pytest.mark.parametrize(
    ("local", "backend", "allowed"),
    [
        (WRITER, WRITER + ["assignment:read"], False),
        (WRITER + ["assignment:read"], WRITER, False),
        (WRITER + ["assignment:read"], WRITER + ["assignment:read"], True),
    ],
)
def test_project_follows_the_gateway_assignment_ceiling(tmp_path, local, backend, allowed):
    from agent_gateway.policy import Denied

    dispatch = gateway(tmp_path, local, backend, tools=PLANNER)
    call = dispatch.call("gateway-a", "calendar_plan", {"actions": [PROJECT]})
    if allowed:
        assert asyncio.run(call)["plan_id"] == "plan-synthetic"
    else:
        with pytest.raises(Denied):
            asyncio.run(call)
    caps = asyncio.run(dispatch.call("gateway-a", "calendar_capabilities", {}))
    assert caps["constraints"] == {"plan_action_limit": 8, "plan_step_limit": 16}


# spec: ANYM-434
@pytest.mark.parametrize(
    ("local", "backend", "allowed"),
    [
        (WRITER, WRITER + ["assignment:read"], False),
        (WRITER + ["assignment:read"], WRITER, False),
        (WRITER + ["assignment:read"], WRITER + ["assignment:read"], True),
    ],
)
def test_unproject_follows_the_gateway_assignment_ceiling(tmp_path, local, backend, allowed):
    from agent_gateway.policy import Denied

    dispatch = gateway(tmp_path, local, backend, tools=PLANNER)
    call = dispatch.call("gateway-a", "calendar_plan", {"actions": [{"action": "unproject", "operation_id": "op-1"}]})
    if allowed:
        assert asyncio.run(call)["plan_id"] == "plan-synthetic"
    else:
        with pytest.raises(Denied):
            asyncio.run(call)


CEILINGS = [
    (WRITER, WRITER + ["assignment:read"], False),
    (WRITER + ["assignment:read"], WRITER, False),
    (WRITER + ["assignment:read"], WRITER + ["assignment:read"], True),
]
LESSON = {"title": "Swim class", "start": "2026-10-20T14:00:00+00:00", "end": "2026-10-20T15:00:00+00:00",
          "timezone": "UTC"}


# spec: ANYM-435
@pytest.mark.parametrize(("local", "backend", "allowed"), CEILINGS)
def test_activity_create_follows_the_gateway_assignment_ceiling(tmp_path, local, backend, allowed):
    from agent_gateway.policy import Denied

    dispatch = gateway(tmp_path, local, backend, tools=PLANNER)
    action = {"action": "create", "calendar_ref": "cal-child", "event": LESSON, "activity": "Dance"}
    call = dispatch.call("gateway-a", "calendar_plan", {"actions": [action]})
    if allowed:
        assert asyncio.run(call)["plan_id"] == "plan-synthetic"
    else:
        with pytest.raises(Denied):
            asyncio.run(call)


# spec: ANYM-435
@pytest.mark.parametrize("carried", [True, False])
def test_carried_drives_never_pass_a_gateway_without_rule_read(tmp_path, carried):
    from agent_gateway.policy import Denied

    lesson = {"action": "update", "calendar_ref": "cal-child", "event_ref": "event-lesson", "event": LESSON}
    linked = {"action": "update", "calendar_ref": "cal-a", "link": {"rule_id": "rule-synthetic"}}
    plan = {"plan_id": "plan-synthetic", "steps": [dict(lesson, ordinal=0)] + ([linked] if carried else [])}
    # The backend reads rules for its key; this gateway's ceiling does not.
    dispatch = gateway(tmp_path, WRITER + ["event:update"], WRITER + ["event:update", "assignment:read"],
                       tools=PLANNER, plan=plan)
    call = dispatch.call("gateway-a", "calendar_plan", {"actions": [lesson]})
    if carried:
        with pytest.raises(Denied):
            asyncio.run(call)
    else:
        # An ordinary lesson change without drives is untouched by the ceiling.
        assert asyncio.run(call)["steps"] == plan["steps"]
