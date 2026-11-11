"""Compatibility and client guidance from Android agent feedback."""

import json

import pytest
from starlette.testclient import TestClient

from agent_gateway.catalog import CATALOG, InvalidArguments, arguments
from test_android import setup
from test_transport import rpc


def compact_policy(tmp_path):
    path = tmp_path / 'policy.json'
    policy = json.loads(path.read_text())
    policy['revision'] += 1
    policy['profiles']['reader']['tools'].extend(['platform_search_tools', 'platform_call_tool'])
    path.write_text(json.dumps(policy))


# spec: ANYM-951
def test_legacy_lease_alias_is_canonical_on_both_mcp_paths(tmp_path):
    app, calls = setup(tmp_path)
    compact_policy(tmp_path)
    with TestClient(app) as client:
        for path in ('/mcp', '/mcp/compact'):
            args = {'device_id': 'phone', 'action': 'renew', 'session_id': 'lease', 'lease_s': 30}
            name = 'android_session'
            if path.endswith('compact'):
                name, args = 'platform_call_tool', {'name': name, 'arguments': args}
            result = client.post(path, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                'params': {'name': name, 'arguments': args}}, headers={
                    'Authorization': 'Bearer gateway-a', 'Accept': 'application/json, text/event-stream'}).json()['result']
            assert not result.get('isError'), result
    assert all(item[2]['seconds'] == 30 and 'lease_s' not in item[2] for item in calls)


# spec: ANYM-951
def test_lease_alias_conflict_is_rejected_and_lookup_defaults_are_discoverable():
    with pytest.raises(InvalidArguments):
        arguments(CATALOG['android_session'], {'device_id': 'phone', 'action': 'renew', 'lease_s': 30, 'seconds': 60})
    fields = CATALOG['android_run_flow']['inputSchema']['properties']
    assert fields['element_timeout_ms']['default'] == 5000
    assert fields['optional_timeout_ms']['default'] == 1000


# spec: ANYM-955
def test_screen_text_and_full_hierarchy_are_discoverable_in_descriptions():
    run, reference = CATALOG['android_run_flow']['description'], CATALOG['android_maestro']['description']
    assert 'readScreenText' in run and 'data.<key>' in run and 'hierarchy {compressed: false' in run
    assert 'reference.screen_text' in reference


# spec: ANYM-956
def test_loading_waits_and_phases_are_discoverable_in_descriptions():
    run = CATALOG['android_run_flow']['description']
    assert 'waitSettled' in run and 'settle_ms' in run and 'phases' in run and 'sleep' in run


# spec: ANYM-957
def test_image_parts_and_download_chunks_are_described():
    observe, download = CATALOG['android_observe']['description'], CATALOG['android_download']['description']
    assert 'two MCP content parts' in observe and 'image_mode=file returns one JSON part' in observe
    assert 'read_next_chunk' in download and 'length=4194304' in download


# spec: ANYM-958
def test_descriptions_name_one_ui_engine():
    assert 'the Maestro engine was removed' in CATALOG['android_capabilities']['description']
    assert 'the only engine' in CATALOG['android_maestro']['description']
    assert 'or legacy maestro' not in CATALOG['android_capabilities']['description']


# spec: ANYM-951
def test_existing_flows_remain_compatible_before_android_backend_upgrade():
    values = {'device_id': 'phone', 'session_id': 'lease',
              'request_id': '00000000-0000-4000-8000-000000000001', 'yaml': '- back'}
    forwarded = arguments(CATALOG['android_run_flow'], values)
    # Older strict backends do not declare the two new fields. Omitted options
    # must stay omitted on the wire; both old and new backends own their defaults.
    assert 'element_timeout_ms' not in forwarded
    assert 'optional_timeout_ms' not in forwarded
    explicit = arguments(CATALOG['android_run_flow'], {**values, 'element_timeout_ms': 400,
                                                       'optional_timeout_ms': 200})
    assert explicit['element_timeout_ms'] == 400 and explicit['optional_timeout_ms'] == 200


# spec: ANYM-952
def test_offline_phone_does_not_break_compact_discovery_and_retry_is_explicit(tmp_path):
    app, _ = setup(tmp_path, unavailable=True)
    compact_policy(tmp_path)
    with TestClient(app) as client:
        for _ in range(3):
            listed = client.post('/mcp/compact', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}, headers={
                'Authorization': 'Bearer gateway-a', 'Accept': 'application/json, text/event-stream'}).json()['result']
            assert {tool['name'] for tool in listed['tools']} == {
                'platform_capabilities', 'platform_search_tools', 'platform_call_tool'}
        result = rpc(client, 'tools/call', {'name': 'android_capabilities', 'arguments': {}}).json()['result']
        error = result['structuredContent']
        assert error['operation_sent'] is False and error['retry_after_seconds'] == 2
        assert error['next_action'] == 'retry_after_delay'
