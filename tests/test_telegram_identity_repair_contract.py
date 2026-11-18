"""ANYM-187 repair contract on a real archive, with no provider: one pass completes at most 100 confirmed private short
sends in sent order, each as a reconcile observation on the authenticated_self basis that leaves revision 1 untouched;
a deleted row or a group row is never chosen, even with a matching sent receipt; the collector repairs before its first
reconcile pass. The provider is a namespace holding only the verified owner, so any lookup on it would fail."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from telethon import types

from messaging_service.envelope import stable_id
from messaging_service.outgoing import OutgoingWorker
from test_archive import event
from test_sending import sender as sender, submit

ACCOUNT = 'send-fixture'
START = datetime(2026, 10, 2, tzinfo=timezone.utc)
OWNER = types.User(id=42, is_self=True, first_name='Synthetic', last_name='Owner', username='fixture_owner')


def at(minutes):
    return (START + timedelta(minutes=minutes)).isoformat()


def short_send(archive, s, receipt, provider_id, sent_at, *, chat='100', chat_type='private', deleted=False):
    """One empty-sender outgoing message plus its own sent receipt cloned from the real confirmed send."""
    archive.ingest(event(account=ACCOUNT, chat=chat, provider_id=provider_id, sender='', body='Exact synthetic text',
        direction='outgoing', sent_at=sent_at, updated_at=sent_at, metadata={'chat_type': chat_type}))
    if deleted:
        archive.ingest(event(account=ACCOUNT, chat=chat, provider_id=provider_id, sender='', body='Exact synthetic text',
            direction='outgoing', sent_at=sent_at, updated_at=at(500), deleted=True, metadata={'chat_type': chat_type}))
    with s.pool.connection() as c:
        c.execute('''INSERT INTO outgoing_messages SELECT (jsonb_populate_record(o,jsonb_build_object(
            'id','send_repair_'||%s,'request_id','synthetic-repair-'||%s,'provider_message_id',%s::text,
            'chat',%s::text,'provider_chat',%s::text))).* FROM outgoing_messages o WHERE o.id=%s''',
            (provider_id, provider_id, provider_id, stable_id('chat', ACCOUNT, chat), chat, receipt))
    return stable_id('msg', ACCOUNT, chat, provider_id)


@pytest.fixture
def batch(sender):
    """101 eligible sends (sent minute i, written newest first) and three ineligible ones sent before all of them.

    An archived deletion blanks the body, so its receipt no longer matches; the third row is deleted with its body
    kept (direct state, no writer makes it today) so that only the selection's own non-deleted rule excludes it."""
    s, archive, key, _, state = sender
    receipt = submit(s, key)['send_id']
    async def send(job): return {'provider_message_id': '42'}
    asyncio.run(OutgoingWorker(state, send).once())
    archive.ingest(event(account=ACCOUNT, chat='200', provider_id='1', sender='7', body='Synthetic group seed',
        metadata={'chat_type': 'group'}))
    ineligible = {'deleted': short_send(archive, s, receipt, '900', at(-30), deleted=True),
                  'group': short_send(archive, s, receipt, '901', at(-20), chat='200', chat_type='group'),
                  'deleted_with_body': short_send(archive, s, receipt, '902', at(-10))}
    with s.pool.connection() as c:
        c.execute('UPDATE messages SET deleted=true WHERE id=%s', (ineligible['deleted_with_body'],))
    eligible = {i: short_send(archive, s, receipt, str(1000 + i), at(i)) for i in reversed(range(101))}
    return s, archive, state, eligible, ineligible


def rows(s, ids):
    with s.pool.connection() as c:
        return {r['id']: r for r in c.execute('''SELECT m.id,m.sender,m.revision,m.deleted,
            (SELECT snapshot FROM message_revisions WHERE message=m.id AND revision=1) AS first,
            (SELECT snapshot FROM message_revisions WHERE message=m.id AND revision=m.revision) AS latest
            FROM messages m WHERE m.id=ANY(%s)''', (list(ids),)).fetchall()}


# spec: ANYM-187
def test_one_pass_repairs_the_100_earliest_eligible_sends_and_the_next_pass_the_101st(batch):
    from messaging_service import telegram_identity
    s, archive, state, eligible, ineligible = batch
    before = rows(s, [*eligible.values(), *ineligible.values()])
    assert telegram_identity.repair_sent_senders(archive, ACCOUNT, state.lease, OWNER) == 100
    after = rows(s, eligible.values())
    assert [i for i, mid in eligible.items() if after[mid]['sender'] == ''] == [100]
    assert telegram_identity.repair_sent_senders(archive, ACCOUNT, state.lease, OWNER) == 1
    assert telegram_identity.repair_sent_senders(archive, ACCOUNT, state.lease, OWNER) == 0
    after = rows(s, [*eligible.values(), *ineligible.values()])
    for mid in eligible.values():
        row = after[mid]
        assert (row['sender'], row['revision']) == ('42', 2) and row['first'] == before[mid]['latest']
        assert row['latest']['origin'] == 'reconcile' and row['latest']['sender'] == '42'
        assert row['latest']['metadata'] == {'chat_type': 'private', 'sender_identity_basis': 'authenticated_self'}
        assert row['latest']['sender_profile'] == {'display_name': 'Synthetic Owner', 'username': 'fixture_owner'}
    for mid in ineligible.values():
        assert after[mid] == before[mid] and after[mid]['sender'] == ''
    assert after[ineligible['deleted']]['deleted'] and after[ineligible['deleted_with_body']]['deleted']
    assert after[ineligible['group']]['latest']['metadata'] == {'chat_type': 'group'}


# spec: ANYM-187
def test_the_collector_repairs_before_its_first_reconcile_pass(batch, tmp_path):
    from messaging_service.collector_state import CaptureSpool
    from messaging_service.telegram_collector import TelegramCollector
    s, archive, state, eligible, _ = batch
    seen = []
    class Stop(BaseException): pass
    async def sync_once():
        seen.append(sorted(r['sender'] for r in rows(s, eligible.values()).values()))
        raise Stop()
    collector = TelegramCollector(archive, ACCOUNT, SimpleNamespace(verified_self=OWNER),
        CaptureSpool(tmp_path / 'capture', reserve_bytes=0))
    collector.state, collector.sync_once = state, sync_once
    with pytest.raises(Stop): asyncio.run(collector.reconcile_loop())
    assert seen == [[''] + ['42'] * 100]
