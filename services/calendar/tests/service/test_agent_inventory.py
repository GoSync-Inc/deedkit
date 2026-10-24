"""Calendar agent inventory and key-file lookup for the admin CLI (ANYM-494): public fields only."""
import hashlib
import json

from family_calendar.service import cli


def dispatch(store, tmp_path, *argv):
    return cli.dispatch(cli.parser().parse_args(list(argv)), tmp_path, tmp_path, {}, store)


def agents(store):
    store.grant("family", "adult-a", "cal-a", ["agenda:read"], "details")
    first = store.agent("planner", "family", "adult-a", ["agenda:read"], ["cal-a"])
    second = store.agent("helper", "family", "adult-a", ["agenda:read"], [])
    store.revoke("helper")
    return first, second


# spec: ANYM-494
def test_list_agents_lists_every_agent_with_public_fields_only(household, tmp_path):
    first, second = agents(household)
    result = dispatch(household, tmp_path, "list-agents")
    rows = result["agents"]
    assert [(r["principal_id"], r["revoked"], r["calendars"]) for r in rows] == [("helper", True, 0), ("planner", False, 1)]
    assert all(set(r) == {"principal_id", "group_id", "person_id", "expires_at", "revoked", "calendars"} for r in rows)
    text = json.dumps(result)
    for secret in (first, second, hashlib.sha256(first.encode()).hexdigest()):
        assert secret not in text


# spec: ANYM-494
def test_agent_info_answers_each_line_of_a_key_file_in_order_without_the_key_or_hash(household, tmp_path):
    first, second = agents(household)
    batch = tmp_path / "batch"
    batch.write_text(f"{second}\nunknown-key\nnot a key\n{first}\n")
    result = dispatch(household, tmp_path, "agent-info", "--key-file", str(batch))
    rows = result["agents"]
    assert [r["found"] for r in rows] == [True, False, False, True]
    assert (rows[0]["principal_id"], rows[0]["revoked"]) == ("helper", True)
    assert set(rows[3]) == {"found", "principal_id", "expires_at", "revoked"} and rows[3]["principal_id"] == "planner"
    assert rows[1] == {"found": False} and rows[2] == {"found": False, "error": "invalid_key"}
    text = json.dumps(result)
    for secret in (first, second, hashlib.sha256(first.encode()).hexdigest()):
        assert secret not in text
