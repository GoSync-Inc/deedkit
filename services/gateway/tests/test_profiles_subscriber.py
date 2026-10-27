"""The messaging-subscriber profile adds the subscription tools to messaging-reader, which stays as it was (ANYM-518)."""
import subprocess
import sys
import tomllib
from pathlib import Path

from agent_gateway.catalog import CATALOG

ROOT = Path(__file__).resolve().parents[3]
READER = ["platform_capabilities", "messages_capabilities", "messages_list_chats", "messages_search",
          "messages_get_messages", "messages_get_context", "messages_get_attachment", "messages_get_updates",
          "messages_ack_updates", "messages_list_subscriptions", "messages_get_subscription",
          "messages_get_subscription_events"]
SUBSCRIBER = READER + ["messages_create_subscription", "messages_update_subscription",
                       "messages_configure_subscription_delivery", "messages_fetch_subscription_events",
                       "messages_wait_for_subscription_events", "messages_ack_subscription_events"]


def tools(name):
    return tomllib.loads((ROOT / "config/profiles" / (name + ".toml")).read_text())["exposure"]["tools"]


# spec: ANYM-518
def test_subscriber_and_reader_profiles_have_exact_tool_sets():
    assert tools("messaging-reader") == READER
    assert tools("messaging-subscriber") == SUBSCRIBER and len(set(SUBSCRIBER)) == len(SUBSCRIBER)
    assert all(name in CATALOG for name in SUBSCRIBER)
    # The profile resolves like the others.
    result = subprocess.run([sys.executable, str(ROOT / "scripts/deedkit.py"), "resolve", "--profile",
                             str(ROOT / "config/profiles/messaging-subscriber.toml"), "--json"],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
