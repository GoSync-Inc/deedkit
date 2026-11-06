"""ANYM-809: the native journal's repair count reaches get_sync_status as counts only."""
import json

import pytest

from messaging_service.archive import Archive
from messaging_service.presentation import render
from messaging_service.whatsapp_collector import (WhatsAppState, WhatsmeowProvider, NATIVE_REPAIR_STATES,
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
    assert repair['counts']['would_add_content'] == 2 and repair['last_error'] == 'repair_storage'
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
