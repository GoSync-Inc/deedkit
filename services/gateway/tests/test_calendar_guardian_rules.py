import asyncio
import json

import pytest

from agent_gateway.dispatch import Dispatch
from agent_gateway.policy import Policy
from test_policy import config

RULE = {"rule_id": "rule-synthetic", "activity": "Dance", "title": "SYNTHETIC-PRIVATE-RULE"}


def gateway(tmp_path, local, backend, readiness=None):
    key = tmp_path / "backend.key"
    key.write_text("backend-calendar-a")
    key.chmod(0o600)
    document = config()
    document["profiles"]["reader"]["tools"] = ["calendar_capabilities", "calendar_members"]
    document["principals"][0]["grants"]["calendar"] = local
    document["principals"][0]["credentials"]["calendar"]["key_file"] = str(key)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(document))
    path.chmod(0o600)

    class Clients:
        deployment = Policy(path).load()["deployment"]

        async def call(self, module, operation, credential, arguments):
            if operation == "capabilities":
                caps = {"permissions": backend, "calendars": [], "features": {"agenda": True}}
                return caps | ({"guardian_projections": readiness} if readiness else {})
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
