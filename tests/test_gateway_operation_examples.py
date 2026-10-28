"""The gateway publishes per-operation examples for the risk-class tools; the messaging backend accepts each one."""
import json
from pathlib import Path

from messaging_service.messenger_operations import OPERATION_TOOLS, operation_spec, validate_operation, _supported

ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = {"telegram-synthetic": ("telegram", (None,)), "whatsapp-synthetic": ("whatsapp", (None, "whatsmeow"))}


# spec: ANYM-550
def test_every_published_operation_example_passes_the_backend_parameter_validator():
    rows = {r["name"]: r for r in json.loads(
        (ROOT / "services/gateway/src/agent_gateway/catalog.json").read_text())}
    for tool, operations in OPERATION_TOOLS.items():
        examples = rows["messages_" + tool]["examples"]
        assert set(examples) == set(operations), tool
        for operation, example in examples.items():
            platform, providers = PLATFORMS[example["account"]]
            spec = operation_spec(operation, platform)
            provider = next(p for p in providers if _supported(platform, operation, spec, p))
            validate_operation(platform, operation, example.get("parameters", {}), provider=provider)
