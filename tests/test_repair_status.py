"""ANYM-809: the native journal's repair count reaches get_sync_status as counts only."""
import asyncio
import json

import pytest

from messaging_service.archive import Archive
from messaging_service.presentation import render
from messaging_service.whatsapp_collector import (WhatsAppCollector, WhatsAppState, WhatsmeowProvider, NATIVE_REPAIR_STATES,
    validate_native_repair_status)


def repair_status(**changes):
    value = {'version': 1, 'done': False, 'rows_scanned': 9,
             'counts': {'would_add_content': 2, 'label_only': 2, 'same': 1, 'no_raw': 1,
                        'placeholder': 1, 'sender_key_only': 1, 'parse_error': 1},
             'last_error': '', 'observed_at': '2026-11-06T06:00:00Z'}
    value.update(changes)
    return value


@pytest.fixture
def native_repair_account(seeded):
    s, *_ = seeded
    a = Archive(s)
    a.register('wa-repair', 'whatsapp', 'Synthetic native repair', discover=True)
    epoch = a.acquire('wa-repair', 'repair-test')
    state = WhatsAppState(a, 'wa-repair', ('repair-test', epoch))
    key = s.issue_key('repair-agent', ['wa-repair'], permissions=['read'])
    yield s, a, state, key, epoch
    a.release('wa-repair', 'repair-test', epoch)


# spec: ANYM-809
def test_repair_status_file_is_counts_and_fixed_codes_only(tmp_path):
    provider = WhatsmeowProvider({'events_directory': str(tmp_path), 'native_executable': '/unused'}, tmp_path/'unused', 3)
    assert provider.repair_status() is None
    path = tmp_path/'.repair-status'
    path.write_text(json.dumps(repair_status(done=True)))
    path.chmod(0o600)
    status = provider.repair_status()
    assert status['done'] is True and list(status['counts']) == list(NATIVE_REPAIR_STATES)
    # Unreadable or out-of-range files are no report, never an exception.
    for text in ('[' * 60000, json.dumps(repair_status(observed_at='0001-01-01T00:00:00+01:00')), '{"version":'):
        path.write_text(text)
        assert provider.repair_status() is None
    with pytest.raises(ValueError, match='invalid_native_repair_status'):
        validate_native_repair_status(repair_status(observed_at='0001-01-01T00:00:00+01:00'))
    for unsafe in (repair_status(body='private text'), repair_status(last_error='message 123 failed'),
                   repair_status(counts={**repair_status()['counts'], 'chat': 1}),
                   repair_status(rows_scanned=-1), repair_status(rows_scanned=True), repair_status(done='yes')):
        with pytest.raises(ValueError, match='invalid_native_repair_status'):
            validate_native_repair_status(unsafe)


# spec: ANYM-809
def test_sync_status_full_shows_the_repair_count_per_account_and_version(native_repair_account):
    s, a, state, key, epoch = native_repair_account
    state.provider_observation('whatsmeow')
    state.native_repair_observation(repair_status(last_error='repair_storage'))
    result = s.get_sync_status(key)
    row = next(x for x in result['sources'] if x['account'] == 'wa-repair')
    repair = row['native_repair']
    assert repair['version'] == 1 and repair['done'] is False and repair['rows_scanned'] == 9
    assert repair['counts'] == repair_status()['counts'] and repair['last_error'] == 'repair_storage'
    assert repair['publishes'] is False and 'provider_repair' not in row['operations']
    full = render('get_sync_status', result, 'full')
    assert next(x for x in full['sources'] if x['account'] == 'wa-repair')['native_repair'] == repair
    compact = render('get_sync_status', result, 'compact')
    assert all('native_repair' not in x for x in compact['sources'])
    # A finished count replaces the earlier one; a provider switch removes it.
    state.native_repair_observation(repair_status(done=True))
    row = next(x for x in s.get_sync_status(key)['sources'] if x['account'] == 'wa-repair')
    assert row['native_repair']['done'] is True and row['native_repair']['last_error'] == ''
    state.provider_observation('webjs')
    row = next(x for x in s.get_sync_status(key)['sources'] if x['account'] == 'wa-repair')
    assert row['native_repair'] is None
    a.release('wa-repair', 'repair-test', epoch)
    with pytest.raises(RuntimeError, match='collector_lease_lost'):
        state.native_repair_observation(repair_status())


def heartbeat_worker(tmp_path, state):
    worker = WhatsAppCollector.__new__(WhatsAppCollector)
    worker.heartbeat_interval = .01
    worker.state = state
    worker.policy = {}
    worker.provider = WhatsmeowProvider({'events_directory': str(tmp_path), 'native_executable': '/unused'}, tmp_path/'unused', 3)
    beats = []
    async def renew(): beats.append(1)
    async def nothing(*_): pass
    worker.renew_lease, worker.refresh_policy, worker.publish_disk_floors = renew, nothing, nothing
    worker.provider.set_policy = nothing
    return worker, beats


# spec: ANYM-809
@pytest.mark.parametrize('text', ['[' * 60000, json.dumps(repair_status(observed_at='0001-01-01T00:00:00+01:00')),
                                  json.dumps(repair_status(counts={'chat': 1}))])
def test_a_malformed_repair_status_never_stops_the_heartbeat(tmp_path, text):
    path = tmp_path/'.repair-status'
    path.write_text(text)
    path.chmod(0o600)
    observed = []
    class State:
        def native_history_observation(self, status): pass
        def native_repair_observation(self, status): observed.append(status)
    worker, beats = heartbeat_worker(tmp_path, State())
    async def scenario():
        beat = asyncio.create_task(worker.heartbeat())
        try:
            for _ in range(500):
                if len(beats) >= 5 or beat.done(): break
                await asyncio.sleep(.01)
            assert not beat.done() and len(beats) >= 5 and observed == []
            # A valid file on a later beat is reported; the heartbeat kept going.
            path.write_text(json.dumps(repair_status()))
            for _ in range(500):
                if observed: break
                await asyncio.sleep(.01)
            assert observed and observed[0]['rows_scanned'] == 9 and not beat.done()
        finally:
            beat.cancel()
            await asyncio.gather(beat, return_exceptions=True)
    asyncio.run(scenario())


# spec: ANYM-809
def test_a_lost_lease_while_reporting_the_repair_count_still_ends_the_heartbeat(tmp_path):
    path = tmp_path/'.repair-status'
    path.write_text(json.dumps(repair_status()))
    path.chmod(0o600)
    class State:
        def native_history_observation(self, status): pass
        def native_repair_observation(self, status): raise RuntimeError('collector_lease_lost')
    worker, _ = heartbeat_worker(tmp_path, State())
    async def scenario():
        await asyncio.wait_for(worker.heartbeat(), 5)
    with pytest.raises(RuntimeError, match='collector_lease_lost'):
        asyncio.run(scenario())
