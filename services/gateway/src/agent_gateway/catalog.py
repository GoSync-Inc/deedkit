"""Reviewed, packaged schemas. New backend methods never appear automatically."""

from copy import deepcopy
import base64
import binascii
from importlib.resources import files
import json
import math

from jsonschema import Draft202012Validator
from jsonschema.validators import extend

from .field_errors import field_errors


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
    "If discovery.catalog in the result differs from the Catalog value ending this description, "
    "your tool list is stale: tell the owner once per session to reconnect the DeedKit MCP client.",
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
}


# JSON Schema treats 1.0 as an integer. The backend's strict integer fields
# reject it, so validate the same wire types before any upstream admission.
StrictValidator = extend(
    Draft202012Validator,
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine_many({
        "integer": lambda checker, value: type(value) is int,
        "number": lambda checker, value: type(value) is int or type(value) is float and math.isfinite(value),
    }),
)


def finite_numbers(value):
    # Check untyped operation parameters too: schema bounds do not reject NaN,
    # and HTTPX would otherwise fail serialization after capability admission.
    pending = [value]
    while pending:
        item = pending.pop()
        if type(item) is float and not math.isfinite(item):
            raise ValueError("invalid_arguments")
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


class InvalidArguments(ValueError):
    """`invalid_arguments` for arguments that fail the tool's schema, with bounded field_errors."""

    def __init__(self, field_errors):
        super().__init__("invalid_arguments")
        self.field_errors = field_errors


FEEDBACK_OPERATION_HINT = "a tool name from tools/list, with or without the messages_ prefix"


def feedback_operation_errors(value):
    """`operation` of submit_feedback must name a reviewed catalog tool, bare or with the `messages_` prefix.

    The static catalog decides, not the modules deployed or visible to the key, so a report about an unavailable
    tool is still accepted (ANYM-342). The submitted name is never echoed.
    """
    name = value.get("operation")
    if isinstance(name, str) and name not in CATALOG and "messages_" + name not in CATALOG:
        return [{"field": "operation", "reason": "enum", "expected": FEEDBACK_OPERATION_HINT}]
    return []


def arguments(tool, value):
    if not isinstance(value, dict):
        raise ValueError("invalid_arguments")
    finite_numbers(value)
    schema = tool["inputSchema"]
    errors = list(StrictValidator(schema).iter_errors(value))
    if errors:
        raise InvalidArguments(field_errors(schema, errors))
    if tool.get("name") == "messages_submit_feedback" and (errors := feedback_operation_errors(value)):
        raise InvalidArguments(errors)
    if tool["operation"] == "upload_file_chunk":
        try:
            raw = base64.b64decode(value["data_base64"], validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("invalid_arguments") from None
        if not 0 < len(raw) <= 262144:
            raise ValueError("invalid_arguments")
    result = deepcopy(value)
    # REST and MCP backend defaults differ for conversation tools. Preserve the
    # reviewed MCP defaults when routing either gateway transport over REST.
    for key, prop in schema.get("properties", {}).items():
        if key not in result and "default" in prop:
            result[key] = deepcopy(prop["default"])
    return result


def public(tool, actions=None):
    result = {
        k: deepcopy(v)
        for k, v in tool.items()
        if k in {"name", "title", "description", "inputSchema", "outputSchema", "annotations", "_meta"}
    }
    if tool.get("action_group") and "operation" in result["inputSchema"].get("properties", {}):
        result["inputSchema"]["properties"]["operation"]["enum"] = sorted(actions or [])
    return result
