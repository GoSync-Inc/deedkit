"""Optional local recordings tools retain every gateway admission ceiling."""
import hashlib
import json

import httpx
import pytest
from starlette.testclient import TestClient

from agent_gateway.app import create_app
from agent_gateway.catalog import CATALOG
from test_policy import config
from test_transport import rpc

TOOLS = ['recordings_search', 'recordings_context', 'recordings_archive_status']
SCOPES = ['recordings:read', 'recordings:content']


def setup(tmp_path, *, grants=SCOPES, backend=SCOPES, missing_tool=False, deployed=True, implemented=True):
    value = config()
    value['deployment'] = {'modules': ['calendar'] + (['recordings'] if deployed else []),
                           'backends': {name: {'url': 'http://' + name} for name in ('calendar', 'recordings')}}
    value['profiles']['reader']['tools'] = ['platform_capabilities', 'calendar_slots', *TOOLS]
    if missing_tool:
        value['profiles']['reader']['tools'].remove('recordings_search')
    actor = value['principals'][0]
    actor['grants']['recordings'] = list(grants)
    for name in ('calendar', 'recordings'):
        key = tmp_path / (name + '.key')
        key.write_text('synthetic-' + name); key.chmod(0o600)
        actor['credentials'][name] = {'key_file': str(key), 'key_sha256': hashlib.sha256(key.read_bytes()).hexdigest()}
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps(value)); path.chmod(0o600)
    seen = []
    def handler(request):
        operation = request.url.path.removeprefix('/v1/')
        if operation == 'capabilities':
            return httpx.Response(200, json={'permissions': list(backend) if request.url.host == 'recordings' else ['availability:read'],
                'tool_manifest': {'names': ['search', 'context', 'archive_status'] if implemented else []}})
        seen.append((request.url.host, operation, json.loads(request.content)))
        return httpx.Response(200, json={'items': [], 'coverage': 'local_observed_unknown'})
    return create_app(path, 'http://testserver', transports={name: httpx.MockTransport(handler)
                      for name in ('calendar', 'recordings')}), seen


# spec: ANYM-652
def test_optional_archive_tools_dispatch_exact_operations_and_defaults(tmp_path):
    app, seen = setup(tmp_path)
    with TestClient(app) as client:
        listed = rpc(client, 'tools/list').json()['result']['tools']
        assert set(TOOLS) <= {tool['name'] for tool in listed}
        for name, args, operation, defaults in [
            ('recordings_search', {'account': 'omi-a', 'query': 'synthetic'}, 'search', {'limit': 10}),
            ('recordings_context', {'account': 'omi-a', 'recording_ref': 'a' * 32}, 'context',
             {'kind': 'transcript', 'page_chars': 4000}),
            ('recordings_archive_status', {'account': 'omi-a'}, 'archive_status', {})]:
            result = rpc(client, 'tools/call', {'name': name, 'arguments': args}).json()['result']
            assert not result.get('isError'), result
            assert seen[-1] == ('recordings', operation, {**args, **defaults})
            assert CATALOG[name]['required_manifest'] and CATALOG[name]['annotations']['readOnlyHint']


# spec: ANYM-652
@pytest.mark.parametrize('ceiling', ['grant', 'backend', 'profile', 'deployment', 'manifest'])
def test_archive_search_needs_every_optional_ceiling_before_content_egress(tmp_path, ceiling):
    kwargs = {'grant': {'grants': ['recordings:read']}, 'backend': {'backend': ['recordings:read']},
              'profile': {'missing_tool': True}, 'deployment': {'deployed': False},
              'manifest': {'implemented': False}}[ceiling]
    app, seen = setup(tmp_path, **kwargs)
    with TestClient(app) as client:
        response = rpc(client, 'tools/call', {'name': 'recordings_search',
                      'arguments': {'account': 'omi-a', 'query': 'private-text'}}).json()['result']
        assert response['isError'] and seen == []
        calendar = rpc(client, 'tools/call', {'name': 'calendar_slots', 'arguments': {
            'member_ids': ['synthetic'], 'duration_minutes': 30, 'start': '2026-10-01T00:00:00Z',
            'end': '2026-10-02T00:00:00Z'}}).json()['result']
        assert not calendar.get('isError'), calendar


# spec: ANYM-652
def test_archive_status_is_available_with_read_permission_only(tmp_path):
    app, seen = setup(tmp_path, grants=['recordings:read'], backend=['recordings:read'])
    with TestClient(app) as client:
        response = rpc(client, 'tools/call', {'name': 'recordings_archive_status',
                      'arguments': {'account': 'omi-a'}}).json()['result']
        assert not response.get('isError') and seen == [('recordings', 'archive_status', {'account': 'omi-a'})]
        for name in ('recordings_search', 'recordings_context'):
            assert name not in {t['name'] for t in rpc(client, 'tools/list').json()['result']['tools']}
