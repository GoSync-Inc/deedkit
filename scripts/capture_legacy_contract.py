#!/usr/bin/env python3
"""Run in one backend's locked environment; no DB/provider/session access.

The fake authenticates only a synthetic key for schema discovery. Resource ACL,
events and original delivery are verified separately in real isolated DB tests.
"""
import argparse
import json
from pathlib import Path
import sys


class SyntheticStore:
    public_url = "http://127.0.0.1:13232"

    def authenticate(self, key):
        if key != "synthetic-contract-key":
            from messaging_service.store import APIError
            raise APIError("unauthorized", 401)
        return "synthetic"

    def one(self, sql, params):
        return {"id": "synthetic", "group_id": "group", "person_id": "person", "scopes": [], "role": "adult", "version": 1}

    def audit_boundary(self, *args):
        pass

    def close(self):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module", choices=["messaging", "calendar"], required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--canonical", action="store_true",
                        help="sorted keys and ASCII escapes: the committed tests/platform/fixtures/messaging-contracts.json format")
    args = parser.parse_args()
    sys.path.insert(0, str(args.source.resolve()))
    from starlette.testclient import TestClient
    if args.module == "messaging":
        from messaging_service.app import create_app
        app = create_app(SyntheticStore(), ["127.0.0.1:13232"])
    else:
        from family_calendar.service.api import create_app
        app = create_app(SyntheticStore())
    with TestClient(app, base_url="http://127.0.0.1:13232") as client:
        headers = {"Authorization": "Bearer synthetic-contract-key", "Accept": "application/json, text/event-stream"}
        response = client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        response.raise_for_status()
        tools = response.json()["result"]["tools"]
        malformed = client.post("/v1/not_an_operation", headers=headers, json={})
        get = client.get("/mcp", headers=headers)
    result = {"format": 1, "module": args.module, "synthetic": True, "tools": tools,
              "transport": {"unknown_rest_status": malformed.status_code, "get_mcp_status": get.status_code},
              "limitations": "Schema/transport snapshot; DB ACL, files, subscriptions and ACK require isolated integration evidence."}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(result, indent=2, sort_keys=True) if args.canonical else json.dumps(result, ensure_ascii=False, indent=2)
    args.output.write_text(text + "\n")
    print(json.dumps({"module": args.module, "tools": len(tools), "transport": result["transport"]}))


if __name__ == "__main__":
    main()
