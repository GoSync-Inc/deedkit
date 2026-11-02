"""Every tool name an agent sees is a valid `operation` of a feedback report, at both layers (no network, no database)."""
from pathlib import Path
import sys
import pytest
from messaging_service.feedback import canonical_operation
from tests.test_deedkit_catalog import gateway_tools

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'services/gateway/src'))
from agent_gateway.tool_arguments import feedback_operation_errors  # noqa: E402

PREFIX = 'messages_'
SPELLINGS = sorted({spelling for name in gateway_tools() for spelling in
    ((name, name[len(PREFIX):]) if name.startswith(PREFIX) else (name,))})


# spec: ANYM-670
def test_every_catalog_tool_is_a_valid_feedback_operation():
    known = set(gateway_tools())
    refused_by_messaging = [name for name in SPELLINGS if canonical_operation(name) is None]
    refused_by_gateway = [name for name in SPELLINGS if feedback_operation_errors({'operation': name}, known)]
    assert refused_by_messaging == [] and refused_by_gateway == []
    # A messaging tool is stored bare; every other module's tool as given.
    for name in known - {'messages_send'}:
        assert canonical_operation(name) == (name[len(PREFIX):] if name.startswith(PREFIX) and name[len(PREFIX):] != 'send' else name)
    # `messages_send` is itself an operation key (ANYM-546), so its bare spelling is stored as that key.
    assert canonical_operation('send') == canonical_operation('messages_send') == 'messages_send'


# spec: ANYM-670
def test_feedback_download_stays_accepted_and_messages_download_is_refused_by_the_gateway():
    known = set(gateway_tools())
    assert canonical_operation('download') == 'download' and not feedback_operation_errors({'operation': 'download'}, known)
    assert 'messages_download' not in known and feedback_operation_errors({'operation': 'messages_download'}, known)


# spec: ANYM-670
@pytest.mark.parametrize('name', ['Search', 'calendar_', 'recordings_', 'recordings_Read', 'messages_nonexistent', 'unknown_tool', 'recordings_' + 'a' * 121])
def test_feedback_operation_unknown_names_stay_rejected(name):
    assert canonical_operation(name) is None
