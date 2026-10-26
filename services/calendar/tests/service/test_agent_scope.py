"""Calendar `agent-scope` for the admin CLI's clone (ANYM-496): member, scopes and calendars, never the key."""
import hashlib
import json

from family_calendar.service import cli


def dispatch(store, tmp_path, *argv):
    return cli.dispatch(cli.parser().parse_args(list(argv)), tmp_path, tmp_path, {}, store)


# spec: ANYM-496
def test_agent_scope_reports_member_scopes_and_calendar_visibility_without_the_key(household, tmp_path):
    household.grant("family", "adult-a", "cal-a", ["agenda:read"], "details")
    key = household.agent("planner", "family", "adult-a", ["agenda:read"], ["cal-a"], "busy_only")
    result = dispatch(household, tmp_path, "agent-scope", "--id", "planner")
    assert {k: result[k] for k in ("found", "principal_id", "group_id", "person_id", "scopes", "revoked")} == {
        "found": True, "principal_id": "planner", "group_id": "family", "person_id": "adult-a",
        "scopes": ["agenda:read"], "revoked": False}
    assert result["calendars"] == [{"ref": "cal-a", "visibility": "busy_only"}]
    text = json.dumps(result)
    assert key not in text and hashlib.sha256(key.encode()).hexdigest() not in text


# spec: ANYM-496
def test_agent_scope_answers_an_unknown_agent_and_check_mode_reports_the_schema(household, tmp_path):
    assert dispatch(household, tmp_path, "agent-scope", "--id", "nobody") == {"found": False, "principal_id": "nobody"}
    assert dispatch(household, tmp_path, "agent-scope", "--id", "planner", "--check")["check"] == "ok"
