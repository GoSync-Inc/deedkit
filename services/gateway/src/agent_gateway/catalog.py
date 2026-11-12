"""Reviewed, packaged schemas. New backend methods never appear automatically."""

from copy import deepcopy
from importlib.resources import files
import json

from jsonschema import Draft202012Validator

from .tool_arguments import (
    InvalidArguments as InvalidArguments,
    StrictValidator as StrictValidator,
    arguments as validate_arguments,
)


def load_catalog(rows):
    result = {}
    for tool in rows:
        if tool["name"] in result or tool["name"] == "platform_capabilities":
            raise ValueError("catalog name collision")
        Draft202012Validator.check_schema(tool["inputSchema"])
        result[tool["name"]] = tool
    return result


CATALOG = load_catalog(json.loads(files(__package__).joinpath("catalog.json").read_text()))
ACTIONS = json.loads(files(__package__).joinpath("actions.json").read_text())
CATALOG["platform_capabilities"] = {
    "name": "platform_capabilities",
    "module": "gateway",
    "operation": "capabilities",
    "scopes": [],
    "description": "Authorized modules, backend availability and connection paths. Read modules_available. Optional when tool schemas are known. "
    "Frequent work uses /mcp/modules/android or /mcp/modules/messaging with the same key for direct calls. "
    "Occasional work uses /mcp/compact with platform_search_tools(detail=full) then platform_call_tool. "
    "Reuse schemas and request IDs. tool_paths lists alternatives. If discovery.catalog differs from Catalog below, "
    "ask once to reconnect the DeedKit MCP client. Coverage may be unknown. "
    "Optional module adds its workflow recipes; since (a changelog_revision) adds whats_new after it. Example: {}",
    "inputSchema": {"type": "object", "properties": {
        # The deployable modules, as dispatch.MODULE_PURPOSES names them (a test holds the two equal).
        "module": {"type": "string", "enum": ["agents", "android", "calendar", "calls", "messaging", "recordings"]},
        "since": {"type": "integer"},
    }, "additionalProperties": False},
    "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
}


def arguments(tool, value):
    return validate_arguments(tool, value, CATALOG)


def scopes_met(tool, granted):
    """Whether `granted` (a backend key's permissions, or an agent's grants for the tool's module) admits the tool: all of
    its `scopes`, or all of its `alt_scopes` alone when it has them (ANYM-989: a maintainer key without `read` runs
    the subscription calls of its own maintainer-only subscription; the backend refuses it any other)."""
    granted = set(granted)
    return set(tool["scopes"]) <= granted or bool(tool.get("alt_scopes")) and set(tool["alt_scopes"]) <= granted


def routed(tool, actions):
    """The actions a tool may carry: a risk-class tool (ANYM-549) only those it routes."""
    routes = tool.get("routes_actions")
    return sorted(set(actions or []) & set(routes)) if routes else sorted(actions or [])


def public(tool, actions=None):
    result = {
        k: deepcopy(v)
        for k, v in tool.items()
        if k in {"name", "title", "description", "inputSchema", "outputSchema", "annotations", "_meta"}
    }
    if tool.get("action_group") and "operation" in result["inputSchema"].get("properties", {}):
        result["inputSchema"]["properties"]["operation"]["enum"] = routed(tool, actions)
        result["description"] = with_example(tool, result["inputSchema"]["properties"]["operation"]["enum"])
    if tool.get("guidance"):
        result["description"] += " " + tool["guidance"]
    return result


def with_example(tool, operations):
    """A risk-class tool's description with its example call for the first operation the key may use (ANYM-550)."""
    description = tool["description"]
    examples = tool.get("examples") or {}
    chosen = next((op for op in operations if op in examples), None)
    if chosen is None or "Example: " not in description:
        return description
    example = examples[chosen]
    call = {"account": example["account"], "operation": chosen, **{k: v for k, v in example.items() if k != "account"}}
    return description[:description.index("Example: ")] + "Example: " + json.dumps(
        call, ensure_ascii=False, separators=(",", ":"))
