"""ANYM-353: the owner raises the daily call cap by answering the agent in chat."""
import asyncio
from datetime import datetime, timedelta, timezone

import httpx2
import pytest
from fastapi.testclient import TestClient
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

import destination_policy as policy
from app import create_app
from core import ServiceError, VoiceService
from outbound import CallingSettings, ExternalBrief
from test_agent_api import registry
from test_destination_policy import AGENT_FOUND, Provider, external, manager, surface
from test_tasks import Archive, HEADERS, owner_headers


def spend(service, units):
    """Durable external attempts that already used `units` of today's cap."""
    for _ in range(units):
        row = service.start(service.prepare(ExternalBrief(**external()), [])['id'])
        service.stop(row['id'], 'synthetic_end')


def raises(service):
    return service.db.execute('SELECT COUNT(*) FROM cap_raises').fetchone()[0]


async def test_cap_raise_binds_owner_words_and_units(tmp_path):
    path, doc, save = registry(tmp_path)
    service = VoiceService(tmp_path / 'data')
    provider = Provider()
    outbound = manager(service, provider)
    api = surface(service, path, outbound)
    spend(service, policy.DAILY_UNITS)
    saved = service.prepare(ExternalBrief(**external()), [])
    with pytest.raises(ServiceError, match='daily_call_limit'):
        await outbound.dial(saved['id'], 'Owner words', origin=AGENT_FOUND)
    # Words without the count, or with another number, are refused and change nothing.
    for words, units in [('Да, разреши ещё звонков', 5), ('Добавь 15 звонков', 5), ('Добавь 105 звонков', 5),
                         ('Добавь 2-5 звонков', 5), ('Добавь 5.5 звонка', 5), ('Добавь 5,5 звонка', 5)]:
        with pytest.raises(ServiceError) as refused:
            await api.call('alice', 'raise_call_cap', {'owner_instruction': words, 'extra_units': units})
        assert refused.value.code == 'units_not_in_owner_instruction', words
    for units in (0, policy.MAX_RAISE_UNITS + 1, '5', True, 2.0):
        with pytest.raises(ServiceError, match='invalid_arguments'):
            await api.call('alice', 'raise_call_cap', {'owner_instruction': 'Добавь 5 звонков', 'extra_units': units})
    assert raises(service) == 0 and outbound.daily_call_units() == policy.DAILY_UNITS
    # The scope that dials is the scope that raises.
    doc['principals'][1]['scopes'] = ['calls:read']
    doc['revision'] += 1
    save()
    with pytest.raises(ServiceError, match='not_found_or_forbidden'):
        await api.call('bob', 'raise_call_cap', {'owner_instruction': 'Добавь 5 звонков', 'extra_units': 5})
    words = 'Да, добавь ещё 5 звонков на сегодня'
    raised = await api.call('alice', 'raise_call_cap', {'owner_instruction': words, 'extra_units': 5})
    assert raised['raise']['owner_instruction'] == words and raised['raise']['principal'] == 'alice'
    assert raised['call_cap']['daily_call_units'] == policy.DAILY_UNITS + 5
    assert raised['call_cap']['daily_calls_remaining'] == 5
    expires = datetime.fromisoformat(raised['raise']['expires_at']) - datetime.fromisoformat(raised['raise']['created_at'])
    assert expires == timedelta(hours=24)
    # A repeated request with the same words is the same raise, never a second one.
    again = await api.call('alice', 'raise_call_cap', {'owner_instruction': words, 'extra_units': 5})
    assert again['raise'] == raised['raise'] and raises(service) == 1
    await outbound.dial(saved['id'], 'Owner words', origin=AGENT_FOUND)
    assert len(provider.dialed) == 1
    service.close()


async def test_cap_raise_is_durable_bounded_and_expires(tmp_path):
    service = VoiceService(tmp_path)
    outbound = manager(service)
    await outbound.raise_cap('Можно ещё 20 звонков', 20, 'alice')
    service.close()
    # Raises are durable rows: a restarted service keeps them.
    service = VoiceService(tmp_path)
    outbound = manager(service)
    assert outbound.daily_call_units() == policy.DAILY_UNITS + 20
    assert [r['extra_units'] for r in outbound.active_raises()] == [20]
    # Never above the ceiling: a raise past it is refused whole, concurrent ones included.
    with pytest.raises(ServiceError) as refused:
        await outbound.raise_cap('Добавь 25', 25, 'alice')
    assert refused.value.code == 'call_cap_ceiling' and refused.value.detail['raisable_units'] == 20
    results = await asyncio.gather(outbound.raise_cap('Добавь 15', 15, 'alice'),
                                   outbound.raise_cap('Ещё 15', 15, 'bob'), return_exceptions=True)
    assert sorted(type(r).__name__ for r in results) == ['ServiceError', 'dict']
    assert outbound.daily_call_units() == policy.DAILY_UNITS + 35
    await outbound.raise_cap('Добавь 5', 5, 'alice')
    assert outbound.daily_call_units() == policy.CAP_CEILING
    with pytest.raises(ServiceError, match='call_cap_ceiling'):
        await outbound.raise_cap('Добавь 1', 1, 'alice')
    spend(service, policy.CAP_CEILING)
    saved = service.prepare(ExternalBrief(**external()), [])
    with pytest.raises(ServiceError, match='daily_call_limit'):
        await outbound.dial(saved['id'], 'Owner words', origin=AGENT_FOUND)
    # After 24 hours a raise stops counting and frees room under the ceiling.
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    service.db.execute("UPDATE cap_raises SET expires_at=? WHERE owner_instruction='Можно ещё 20 звонков'", (past,))
    service.db.commit()
    assert outbound.daily_call_units() == policy.CAP_CEILING - 20
    assert len(outbound.active_raises()) == 2
    service.close()


def test_cap_raise_is_visible_to_owner(tmp_path):
    path, _, _ = registry(tmp_path)
    app = create_app(tmp_path / 'data', Archive(), agent_registry=path)
    with TestClient(app) as client:
        client.headers.update(owner_headers(tmp_path / 'data'))
        client.post('/api/credentials', json={'gemini': 'synthetic-model-key'}, headers=HEADERS)
        setup = {'enabled': True, 'twilio': {'account_sid': 'AC' + '1' * 32, 'auth_token': 'synthetic-token',
                                             'phone_from': '+12025550125', 'public_url': 'https://voice.example.test'}}
        assert client.post('/api/calling', json=setup, headers=HEADERS).status_code == 200
        alice, bob = {'Authorization': 'Bearer alice'}, {'Authorization': 'Bearer bob'}
        words = 'Ок, ещё 7 звонков для клиники'
        response = client.post('/v1/raise_call_cap', json={'owner_instruction': words, 'extra_units': 7}, headers=alice)
        assert response.status_code == 200, response.text
        refused = client.post('/v1/raise_call_cap', json={'owner_instruction': 'Ок, ещё звонков', 'extra_units': 7}, headers=alice)
        assert refused.status_code == 422 and refused.json()['error'] == 'units_not_in_owner_instruction'
        # The owner UI shows every raise with the owner's quoted words.
        cap = client.get('/api/config', headers=HEADERS).json()['call_cap']
        assert cap['daily_call_units'] == policy.DAILY_UNITS + 7 and cap['call_cap_ceiling'] == policy.CAP_CEILING
        assert [(r['owner_instruction'], r['extra_units'], r['principal']) for r in cap['raises']] == [(words, 7, 'alice')]
        # Agents see the effective cap; another principal sees the raise without the owner's words.
        own = client.post('/v1/capabilities', json={}, headers=alice).json()
        assert own['call_cap']['raises'][0]['owner_instruction'] == words
        assert own['calling_readiness']['channels']['phone']['daily_calls_remaining'] == policy.DAILY_UNITS + 7
        assert own['call_cap']['call_cap_ceiling'] == policy.CAP_CEILING
        other = client.post('/v1/capabilities', json={}, headers=bob).json()['call_cap']
        assert other['daily_call_units'] == policy.DAILY_UNITS + 7
        assert other['raises'][0]['extra_units'] == 7 and 'owner_instruction' not in other['raises'][0]
        brief = client.post('/v1/prepare_external_call', json={'brief': external()}, headers=alice).json()
        ready = client.post('/v1/preflight_call', json={'brief_id': brief['id']}, headers=alice).json()
        assert ready['daily_call_units'] == policy.DAILY_UNITS + 7 and ready['daily_calls_remaining'] == policy.DAILY_UNITS + 7
        assert ready['call_cap']['raises'][0]['owner_instruction'] == words
    app.state.service.close()


async def test_control_mcp_raises_cap_with_owner_words(tmp_path, monkeypatch):
    monkeypatch.setenv('VOICE_EXTERNAL_MCP', '1')
    app = create_app(tmp_path / 'control', Archive(), control_token='control-fixture-token')
    app.state.outbound.settings = CallingSettings(enabled=True)
    app.state.outbound.providers['twilio'] = Provider()
    app.state.outbound.model_key = lambda b: 'synthetic'
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app),
                                      headers={'Authorization': 'Bearer control-fixture-token'}) as http:
            async with streamable_http_client('http://testserver/control/mcp', http_client=http) as (read, write):
                async with ClientSession(read, write) as client:
                    await client.initialize()
                    tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                    assert 'never raises the cap' in tools['raise_call_cap'].description
                    unbound = await client.call_tool('raise_call_cap', {'owner_instruction': 'Добавь звонков', 'extra_units': 3})
                    assert unbound.is_error and 'units_not_in_owner_instruction' in unbound.content[0].text
                    raised = await client.call_tool('raise_call_cap', {'owner_instruction': 'Добавь 3 звонка', 'extra_units': 3})
                    assert not raised.is_error and raised.structured_content['raise']['principal'] == 'control_mcp'
                    caps = (await client.call_tool('get_voice_capabilities', {})).structured_content['calling']
                    assert caps['daily_call_units'] == policy.DAILY_UNITS + 3
                    assert caps['call_cap']['raises'][0]['owner_instruction'] == 'Добавь 3 звонка'
                    prepared = await client.call_tool('prepare_external_call', {'brief': external()})
                    ready = await client.call_tool('preflight_call', {'brief_id': prepared.structured_content['id']})
                    assert ready.structured_content['call_cap']['daily_calls_remaining'] == policy.DAILY_UNITS + 3


def test_agent_facing_text_states_the_cap_raise_rule():
    from agent_api import DESCRIPTIONS
    from control import create_control
    import agent_mcp
    text = DESCRIPTIONS['raise_call_cap']
    for phrase in ("owner's own reply verbatim", 'extra_units as digits', 'never raises the cap', 'call_cap_ceiling'):
        assert phrase in text, phrase
    source = open(agent_mcp.__file__, encoding='utf-8').read() + open(create_control.__code__.co_filename, encoding='utf-8').read()
    assert source.count('never raises the') >= 2 and 'raise_call_cap' in source
