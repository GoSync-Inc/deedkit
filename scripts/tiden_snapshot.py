#!/usr/bin/env python3
"""Read-only Tiden baseline, using the installed CLI's documented JSON commands.

Private output only. No registry writes, implicit binding or credentials in argv.
Two complete inventories must agree; a page count alone never proves completeness.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

TIMEOUT = 90
RETRY_DELAY = 2  # seconds before the one retry of a command
RETRY_MIN = 10  # a retry needs at least this many seconds left of the command's one TIMEOUT budget


class SnapshotError(RuntimeError):
    pass


SET_PATHS = {(name,) for name in ("tests", "requirements", "links", "components", "products", "branches")} | {
    ("requirements", "[]", "sources"), ("components", "[]", "componentPaths"),
}


def canonical(value, path=()):
    if isinstance(value, dict):
        return {k: canonical(v, (*path, k)) for k, v in sorted(value.items())}
    if isinstance(value, list):
        normalized = [canonical(v, (*path, "[]")) for v in value]
        # Only known inventory collections are sets. Preserve order in steps,
        # schema enums or any future structured content returned by the API.
        return sorted(normalized, key=lambda x: json.dumps(x, sort_keys=True)) if path in SET_PATHS else normalized
    return value


def fingerprint(value):
    return hashlib.sha256(json.dumps(canonical(value), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def stable(first, second):
    if fingerprint(first) != fingerprint(second):
        raise SnapshotError("inventory changed between complete reads; retry in a stable window")
    return canonical(second)


def pages(fetch, key, limit=10000):
    cursor, seen, records, ids, metadata = "", set(), [], set(), None
    for _ in range(limit):
        if cursor in seen:
            raise SnapshotError("repeated pagination cursor")
        seen.add(cursor)
        result = fetch(cursor)
        pagination = result.get("pagination")
        if not isinstance(pagination, dict) or not isinstance(result.get(key), list):
            raise SnapshotError("missing collection/pagination")
        total, next_cursor = pagination.get("totalCount"), pagination.get("nextPageToken")
        if type(total) is not int or total < 0 or not isinstance(next_cursor, str):
            raise SnapshotError("missing completeness metadata")
        current = (total, result.get("revision"))
        if metadata is not None and current != metadata:
            raise SnapshotError("revision or count changed while paging")
        metadata = current
        for record in result[key]:
            if not isinstance(record, dict) or not isinstance(record.get("id"), str) or not record["id"]:
                raise SnapshotError("missing record identity")
            if record["id"] in ids:
                raise SnapshotError("duplicate record across pages")
            ids.add(record["id"])
            records.append(record)
        if not next_cursor:
            if len(records) != total:
                raise SnapshotError("incomplete inventory")
            return records
        cursor = next_cursor
    raise SnapshotError("pagination limit exceeded; incomplete inventory")


class CLI:
    def __init__(self, repo, product_id, workspace_id, branch):
        self.repo, self.product_id, self.workspace_id, self.branch = repo, product_id, workspace_id, branch

    def call(self, *args):
        """A read-only Tiden command; a failed one is repeated once inside the same TIMEOUT budget."""
        start = time.monotonic()
        try:
            return self.once(*args)
        except (SnapshotError, subprocess.TimeoutExpired):
            left = TIMEOUT - (time.monotonic() - start) - RETRY_DELAY
            if left < RETRY_MIN:
                raise
        time.sleep(RETRY_DELAY)
        return self.once(*args, timeout=left)

    def once(self, *args, timeout=None):
        result = subprocess.run(["tiden", *args, "--format", "json", "--workspace-id", self.workspace_id],
                                cwd=self.repo, capture_output=True, text=True, timeout=timeout or TIMEOUT)
        if result.returncode:
            # CLI error text may contain tokens or response bodies. Never echo it.
            raise SnapshotError("read-only Tiden command failed: " + " ".join(args[:2]))
        try:
            value = json.loads(result.stdout)
        except ValueError:
            raise SnapshotError("Tiden returned invalid JSON") from None
        if not isinstance(value, dict):
            raise SnapshotError("unexpected Tiden response")
        return value

    def inventory(self):
        product = ("--product-id", self.product_id)
        branch = ("--branch", self.branch)
        tests = pages(lambda c: self.call("test", "list", *product, *branch, "--view", "detail",
                      "--page-size", "200", *(["--page-token", c] if c else [])), "tests")
        # include-sources is documented as a complete export, still require a terminal cursor/count.
        requirements = pages(lambda c: self.call("spec", "list", *product, *branch, "--include-sources",
                             *(["--page-token", c] if c else [])), "requirements")
        for req in requirements:
            # The protobuf JSON API omits zero counts and empty arrays (verified
            # against spec get). Nonempty arrays still require an exact count.
            sources = req.get("sources", [])
            if not isinstance(sources, list) or req.get("sourceCount", 0) != len(sources):
                raise SnapshotError("incomplete requirement source inventory")
        components = pages(lambda c: self.call("component", "list", *product), "components", limit=1)
        products = pages(lambda c: self.call("product", "list"), "products", limit=1)
        products = [p for p in products if p["id"] == self.product_id]
        if len(products) != 1:
            raise SnapshotError("requested product unavailable")
        branch_result = self.call("branch", "list", *product)
        branches = branch_result.get("branches")
        if not isinstance(branches, list) or not any(b.get("name") == self.branch for b in branches):
            raise SnapshotError("requested branch unavailable")
        pagination = branch_result.get("pagination", {})
        if pagination.get("nextPageToken"):
            raise SnapshotError("branch export truncated")

        def links_for(test):
            result = self.call("test", "links", test["id"], *branch)
            links = result.get("links", [])
            if not isinstance(links, list) or len(links) != test.get("linkedRequirementCount", 0):
                raise SnapshotError("incomplete test links")
            if any(link.get("testId") != test["id"] or not link.get("id") or not link.get("requirementId") for link in links):
                raise SnapshotError("malformed test link")
            return links

        with ThreadPoolExecutor(max_workers=6) as pool:
            links = [link for group in pool.map(links_for, tests) for link in group]
        if len({link["id"] for link in links}) != len(links):
            raise SnapshotError("duplicate link identity")
        return canonical(dict(tests=tests, requirements=requirements, links=links,
                              components=components, products=products, branches=branches))


def private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--branch", default="main")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    cli = CLI(args.repo, config["productId"], config["workspaceId"], args.branch)
    started = datetime.now(timezone.utc).isoformat()
    print("Reading first complete inventory", flush=True)
    first = cli.inventory()
    print("Reading second complete inventory", flush=True)
    second = cli.inventory()
    inventory = stable(first, second)
    result = {"format": 1, "complete": True, "stable_reads": 2, "started_at": started,
              "finished_at": datetime.now(timezone.utc).isoformat(), "branch": args.branch,
              "product_id": cli.product_id, "workspace_id": cli.workspace_id,
              "sha256": fingerprint(inventory), "inventory": inventory}
    private_json(args.output, result)
    print(json.dumps({"sha256": result["sha256"], "counts": {k: len(v) for k, v in inventory.items()}}))


if __name__ == "__main__":
    try:
        main()
    except (SnapshotError, KeyError) as error:
        raise SystemExit(str(error)) from None
