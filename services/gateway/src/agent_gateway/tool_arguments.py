"""Strict wire-argument validation for the reviewed gateway tools."""

from copy import deepcopy
import base64
import binascii
import math

from jsonschema import Draft202012Validator
from jsonschema.validators import extend

from .field_errors import field_errors


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


# A bare messaging operation that feedback accepted before ANYM-342 but that has no gateway tool: keep it valid so
# a recorded request can still be replayed through the gateway. It makes no tool callable.
LEGACY_FEEDBACK_OPERATIONS = frozenset({"download"})
FEEDBACK_OPERATION_HINT = "a tool name from tools/list, with or without the messages_ prefix"


def feedback_operation_errors(value, known_tools):
    """`operation` of submit_feedback must name a reviewed catalog tool, bare or with the `messages_` prefix.

    The static catalog decides, not the modules deployed or visible to the key, so a report about an unavailable
    tool is still accepted (ANYM-342). The submitted name is never echoed.
    """
    name = value.get("operation")
    if isinstance(name, str) and name not in known_tools and "messages_" + name not in known_tools and name not in LEGACY_FEEDBACK_OPERATIONS:
        return [{"field": "operation", "reason": "enum", "expected": FEEDBACK_OPERATION_HINT}]
    return []


def arguments(tool, value, known_tools):
    if not isinstance(value, dict):
        raise ValueError("invalid_arguments")
    finite_numbers(value)
    schema = tool["inputSchema"]
    errors = list(StrictValidator(schema).iter_errors(value))
    if errors:
        raise InvalidArguments(field_errors(schema, errors))
    if tool.get("name") == "messages_submit_feedback" and (errors := feedback_operation_errors(value, known_tools)):
        raise InvalidArguments(errors)
    if tool.get("routes_actions") and not value.get("dry_run") and "request_id" not in value:
        # ANYM-549: a live call of a risk-class tool needs the caller's own key; dry_run neither needs nor uses it.
        raise InvalidArguments([{"field": "request_id", "reason": "required"}])
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

