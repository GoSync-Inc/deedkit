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
    "description": "Selected modules and tools for this agent. Backend data coverage remains explicit/unknown. "
    "tool_paths names every tool you may use with the connection path that lists it; a path with advanced also "
    "lists the low-level tools. "
    "If discovery.catalog in the result differs from the Catalog value ending this description, "
    "your tool list is stale: tell the owner once per session to reconnect the DeedKit MCP client. Example: {}",
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
}


def arguments(tool, value):
    return validate_arguments(tool, value, CATALOG)


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
