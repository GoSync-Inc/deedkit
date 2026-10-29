"""Narrow collector, indexer and enrichment database roles (ANYM-254)."""
from contextlib import contextmanager
import hashlib
from pathlib import Path
import re
import threading
import time
from types import SimpleNamespace as NS
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
import pytest

from messaging_service import store as store_module
from messaging_service.archive import Archive
from messaging_service.blobs import BlobWriter
from messaging_service.enrichment_providers import normalize_result
from messaging_service.store import Store
from test_archive import archive, event, media_event  # noqa: F401 (archive is a fixture)
from test_enrichment import add_audio, worker as enrichment_worker
from test_file_index import add_file, drain, worker_for

SOURCE = Path(store_module.__file__).parent
FORBIDDEN = psycopg.errors.InsufficientPrivilege


def install(store, template, base, account=None):
    role = 'anym254_' + base.removeprefix('messaging_') + '_' + uuid4().hex[:10]
    with store.pool.connection() as c:
        c.execute(SOURCE.joinpath(template).read_text().replace(base, role))
        if account:
            c.execute('INSERT INTO collector_account_roles(role_name,account) VALUES(%s,%s)', (role, account))
    return role


def remove(store, role):
    with store.pool.connection() as c:
        c.execute('DELETE FROM collector_account_roles WHERE role_name=%s', (role,))
        c.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
        c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


@contextmanager
def as_role(store, role):
    with store.pool.connection() as c:
        c.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
        yield c


def refused(store, role, statement, params=()):
    with pytest.raises(FORBIDDEN):
        with as_role(store, role) as c:
            c.execute(statement, params)


class RolePool:
    def __init__(self, store, role):
        self.store, self.role = store, role

    def connection(self):
        return as_role(self.store, self.role)


def processing_event(store, attachment, kind):
    with store.pool.connection() as c:
        return c.execute('''SELECT * FROM events WHERE kind=%s
            AND snapshot->'processing'->>'attachment_id'=%s''', (kind, attachment)).fetchone()


def publish(store, role, function, account, message, kind, event_key, snapshot):
    with as_role(store, role) as c:
        return c.execute(sql.SQL('SELECT public.{}(%s,%s,%s,%s,%s) AS id').format(sql.Identifier(function)),
            (account, message, kind, event_key, Jsonb(snapshot))).fetchone()['id']


def forged_key():
    return 'processing_' + uuid4().hex * 2


def published_snapshot(row):
    # The event trigger may append native metadata; publishers never send it.
    return {k: v for k, v in row['snapshot'].items() if k != 'metadata'}


# spec: ANYM-254
def test_indexer_publishes_text_readiness_only_through_the_fenced_function(seeded):
    s, _, _ = seeded
    drain(worker_for(s))
    role = install(s, 'indexer_role.sql', 'messaging_indexer')
    try:
        worker = worker_for(s)
        worker.connection = lambda: as_role(s, role)
        add_file(s, 'file-anym254', 'demo-tg-1', b'fenced readiness text')
        assert worker.run_once()['status'] == 'ready'
        row = processing_event(s, 'file-anym254', 'file.text.ready')
        with s.pool.connection() as c:
            message = c.execute("SELECT * FROM messages WHERE id='demo-tg-1'").fetchone()
            newest = c.execute('SELECT max(id) AS id,(SELECT value FROM feed_counter) AS counter FROM events').fetchone()
            origin = c.execute('''SELECT coalesce(r.snapshot->>'origin','reconcile') AS origin FROM messages m
                LEFT JOIN message_revisions r ON r.message=m.id AND r.revision=m.revision
                WHERE m.id='demo-tg-1' ''').fetchone()['origin']
        assert row['id'] == newest['id'] == newest['counter'] and row['account'] == 'telegram-demo'
        assert row['origin'] == origin and row['revision'] == message['revision']
        snapshot = published_snapshot(row)
        assert {k: snapshot[k] for k in ('message', 'account', 'chat', 'sender', 'body', 'revision')} == {
            'message': 'demo-tg-1', 'account': 'telegram-demo', 'chat': message['chat'],
            'sender': message['sender'], 'body': message['body'], 'revision': message['revision']}
        assert snapshot['processing']['stage'] == 'text' and snapshot['processing']['state'] == 'ready'

        # No direct journal or counter writes remain.
        refused(s, role, '''INSERT INTO events(id,account,message,kind,revision,origin,snapshot)
            VALUES(1000000,'telegram-demo','demo-tg-1','message.created',1,'live','{}')''')
        refused(s, role, 'UPDATE feed_counter SET value=value+1 WHERE id=1')
        refused(s, role, "UPDATE events SET kind='message.deleted' WHERE id=%s", (row['id'],))
        # Another worker's function, and the function's internal core, are not executable.
        for function in ('publish_file_enrichment_event',):
            with pytest.raises(FORBIDDEN):
                publish(s, role, function, 'telegram-demo', 'demo-tg-1', 'file.transcript.ready',
                        forged_key(), {**snapshot, 'processing': {**snapshot['processing'], 'stage': 'transcript'}})
        refused(s, role, '''SELECT public.publish_processing_event(ARRAY['text','transcript'],
            'telegram-demo','demo-tg-1','file.text.ready',%s,%s)''', (forged_key(), Jsonb(snapshot)))

        forged = {
            'message_kind': ('message.created', snapshot),
            'stage_kind_mismatch': ('file.description.ready', snapshot),
            'foreign_stage': ('file.transcript.ready',
                              {**snapshot, 'processing': {**snapshot['processing'], 'stage': 'transcript'}}),
            'changed_body': ('file.text.ready', {**snapshot, 'body': 'forged body'}),
            'changed_sender': ('file.text.ready', {**snapshot, 'sender': 'someone-else'}),
            'old_revision': ('file.text.ready', {**snapshot, 'revision': message['revision'] + 1}),
            'extra_key': ('file.text.ready', {**snapshot, 'reply_evidence': {'state': 'known'}}),
            'foreign_attachment': ('file.text.ready',
                                   {**snapshot, 'processing': {**snapshot['processing'], 'attachment_id': 'demo-file-1'}}),
            'forged_formatting': ('file.text.ready', {**snapshot, 'formatting': {'state': 'known'}}),
            'extra_processing_key': ('file.text.ready',
                                     {**snapshot, 'processing': {**snapshot['processing'], 'body': 'x'}}),
        }
        for name, (kind, body) in forged.items():
            with pytest.raises(FORBIDDEN):
                publish(s, role, 'publish_file_text_event', 'telegram-demo', 'demo-tg-1', kind, forged_key(), body)
        # The other account's message cannot carry this occurrence.
        wa = {**snapshot, 'message': 'demo-wa-1', 'account': 'whatsapp-demo-1'}
        with pytest.raises(FORBIDDEN):
            publish(s, role, 'publish_file_text_event', 'whatsapp-demo-1', 'demo-wa-1', 'file.text.ready', forged_key(), wa)
        with pytest.raises(FORBIDDEN):
            publish(s, role, 'publish_file_text_event', 'telegram-demo', 'demo-tg-1', 'file.text.ready',
                    'message_' + 'a' * 64, snapshot)
        # A replayed key returns the committed event; a valid new artifact appends after it.
        assert publish(s, role, 'publish_file_text_event', 'telegram-demo', 'demo-tg-1', 'file.text.ready',
                       row['event_key'], snapshot) == row['id']
        again = publish(s, role, 'publish_file_text_event', 'telegram-demo', 'demo-tg-1', 'file.text.ready',
                        forged_key(), snapshot)
        assert again == row['id'] + 1
        with s.pool.connection() as c:
            assert c.execute('SELECT max(id) AS id FROM events').fetchone()['id'] == again
    finally:
        remove(s, role)


# spec: ANYM-254
def test_text_readiness_without_formatting_proof_is_published_through_the_function(seeded):
    s, _, _ = seeded
    drain(worker_for(s))
    role = install(s, 'indexer_role.sql', 'messaging_indexer')
    try:
        with s.pool.connection() as c:
            c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at)
                VALUES('anym254-unproven','telegram-demo','telegram-demo-chat','anym254-unproven','demo-person-1',
                'Synthetic message without revision proof','2026-10-05T10:11:12.5+00:00')''')
        add_file(s, 'file-unproven', 'anym254-unproven', b'unproven formatting text')
        worker = worker_for(s)
        worker.connection = lambda: as_role(s, role)
        assert worker.run_once()['status'] == 'ready'
        row = processing_event(s, 'file-unproven', 'file.text.ready')
        assert row['origin'] == 'reconcile' and row['snapshot']['formatting']['state'] == 'unknown'
    finally:
        remove(s, role)


# spec: ANYM-254
def test_enricher_publishes_model_readiness_only_through_its_function(seeded, tmp_path):
    s, _, _ = seeded
    add_audio(s)
    role = install(s, 'enrichment_role.sql', 'messaging_enricher')
    try:
        w = enrichment_worker(s, tmp_path)
        w.connection = lambda: as_role(s, role)
        with s.pool.connection() as c:
            c.execute('TRUNCATE enrichment_budget')
        row, _ = w.claim()
        assert w.finish(row, normalize_result({'text': 'synthetic transcript'}, audio=True))
        published = processing_event(s, 'enrich-audio', 'file.transcript.ready')
        assert published and published['account'] == 'telegram-demo'
        snapshot = published_snapshot(published)
        refused(s, role, '''INSERT INTO events(id,account,message,kind,revision,origin,snapshot)
            VALUES(1000001,'telegram-demo','demo-tg-2','message.created',1,'live','{}')''')
        refused(s, role, 'UPDATE feed_counter SET value=value+1 WHERE id=1')
        with pytest.raises(FORBIDDEN):
            publish(s, role, 'publish_file_text_event', 'telegram-demo', 'demo-tg-2', 'file.text.ready', forged_key(),
                    {**snapshot, 'processing': {**snapshot['processing'], 'stage': 'text'}})
        with pytest.raises(FORBIDDEN):
            publish(s, role, 'publish_file_enrichment_event', 'telegram-demo', 'demo-tg-2', 'file.text.ready',
                    forged_key(), {**snapshot, 'processing': {**snapshot['processing'], 'stage': 'text'}})
        with pytest.raises(FORBIDDEN):
            publish(s, role, 'publish_file_enrichment_event', 'telegram-demo', 'demo-tg-2', 'message.edited',
                    forged_key(), snapshot)
    finally:
        remove(s, role)


def other_account(ar, s):
    ar.register('archive-other', 'telegram', 'Synthetic other', include_chats=['200'])
    mid = ar.ingest(media_event(account='archive-other', chat='200', provider_id='5'))['message']
    with s.pool.connection() as c:
        for account, chat in [('archive-test', '100'), ('archive-other', '200')]:
            c.execute('''INSERT INTO collector_chats(account,provider_chat,peer_kind) VALUES(%s,%s,'chat')
                ON CONFLICT DO NOTHING''', (account, chat))
            c.execute('''INSERT INTO whatsapp_checkpoints(account,provider_chat) VALUES(%s,%s)
                ON CONFLICT DO NOTHING''', (account, chat))
            c.execute("INSERT INTO source_status(account) VALUES(%s) ON CONFLICT DO NOTHING", (account,))
        job = c.execute('SELECT id FROM media_jobs WHERE message=%s', (mid,)).fetchone()['id']
        # A finished file of the other account, so updates of it match a row.
        c.execute('''INSERT INTO attachments(id,message,digest,filename,mime,bytes)
            VALUES('anym254-other-file',%s,repeat('b',64),'other.txt','text/plain',1)''', (mid,))
        c.execute('''INSERT INTO collector_leases(account,owner,epoch,expires_at)
            VALUES('archive-other','other-collector',1,now()+interval '1 hour') ON CONFLICT DO NOTHING''')
    return mid, job


# spec: ANYM-254
def test_collector_bound_to_one_account_ingests_it_but_cannot_write_another(archive):  # noqa: F811
    ar, s, key, _ = archive
    other, other_job = other_account(ar, s)
    role = install(s, 'collector_role.sql', 'messaging_collector', account='archive-test')
    try:
        bound = Archive(NS(pool=RolePool(s, role), append_event=s.append_event, files=s.files))
        # The allowed path: ingest, edit, media publication and checkpoints of its own account.
        mine = bound.ingest(media_event())
        assert mine['outcome'] == 'created'
        assert bound.ingest(media_event(body='edited', updated_at='2026-10-01T12:01:00Z'))['outcome'] == 'edited'
        job = bound.claim_media('bound-worker', account='archive-test')
        writer = BlobWriter(s.files, reserve_bytes=0)
        bound.finish_media(job, writer.write([b'data'], expected_bytes=4), writer)
        with as_role(s, role) as c:
            c.execute("UPDATE whatsapp_checkpoints SET progress='{\"synthetic\":1}' WHERE account='archive-test'")
            c.execute("UPDATE collector_chats SET high_water=17 WHERE account='archive-test'")
            c.execute("UPDATE source_status SET status='receiving' WHERE account='archive-test'")
        assert [i['kind'] for i in s.get_updates(key, 'bound')['items']] == [
            'message.created', 'message.edited', 'attachment.ready']

        # Another account: message, event, checkpoint and dependent rows are refused.
        with pytest.raises(FORBIDDEN):
            bound.ingest(event(account='archive-other', chat='200', provider_id='6'))
        with pytest.raises(FORBIDDEN):
            with as_role(s, role) as c:
                s.append_event(c, 'archive-other', other, 'message.edited', 'live')
        for statement, params in [
            ("UPDATE messages SET body='forged' WHERE id=%s", (other,)),
            # A row of its own account cannot be moved to another account.
            ("UPDATE messages SET account='archive-other',chat='archive-other-chat' WHERE id=%s", (mine['message'],)),
            ('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at)
                SELECT 'anym254-forged',account,chat,'forged',sender,body,sent_at FROM messages WHERE id=%s''', (other,)),
            ("UPDATE whatsapp_checkpoints SET progress='{}' WHERE account='archive-other'", ()),
            ("INSERT INTO whatsapp_checkpoints(account,provider_chat) VALUES('archive-other','201')", ()),
            ("INSERT INTO collector_chats(account,provider_chat,peer_kind) VALUES('archive-other','201','chat')", ()),
            ("UPDATE collector_chats SET high_water=99 WHERE account='archive-other'", ()),
            ("UPDATE source_status SET status='failed' WHERE account='archive-other'", ()),
            ("UPDATE ingest_receipts SET outcome='forged' WHERE account='archive-other'", ()),
            ('''INSERT INTO attachments(id,message,digest,filename,mime,bytes)
                VALUES('anym254-forged',%s,repeat('a',64),'x.txt','text/plain',1)''', (other,)),
            ("UPDATE attachments SET filename='forged.txt' WHERE message=%s", (other,)),
            ("UPDATE message_revisions SET snapshot='{}' WHERE message=%s", (other,)),
            ("UPDATE media_jobs SET attempts=attempts WHERE id=%s", (other_job,)),
            ('''INSERT INTO media_reservations(job,volume_key,lease_epoch,reserved_bytes,expires_at)
                VALUES(%s,'forged',1,1,now()+interval '1 hour')''', (other_job,)),
            ("UPDATE collector_leases SET owner='forged' WHERE account='archive-other'", ()),
        ]:
            refused(s, role, statement, params)
        # Its binding is not self-service.
        for statement in ["UPDATE collector_account_roles SET account='archive-other'",
                          'DELETE FROM collector_account_roles',
                          "INSERT INTO collector_account_roles(role_name,account) VALUES('x','archive-other')"]:
            refused(s, role, statement)
        with s.pool.connection() as c:
            assert c.execute("SELECT body FROM messages WHERE id=%s", (other,)).fetchone()['body'] == 'Первый текст lighthouse'
    finally:
        remove(s, role)


# spec: ANYM-254
def test_unbound_collector_role_keeps_its_current_rights_for_a_rolling_upgrade(archive):  # noqa: F811
    ar, s, _, _ = archive
    other, _ = other_account(ar, s)
    role = install(s, 'collector_role.sql', 'messaging_collector')
    try:
        shared = Archive(NS(pool=RolePool(s, role), append_event=s.append_event, files=s.files))
        assert shared.ingest(media_event())['outcome'] == 'created'
        assert shared.ingest(event(account='archive-other', chat='200', provider_id='6'))['outcome'] == 'created'
    finally:
        remove(s, role)


@pytest.fixture
def schema36_store(database, tmp_path):
    """An independent database built from the unchanged schema-36 SQL and checksums."""
    name = 'anymsg_roles_' + uuid4().hex + '_test'
    dsn = database.pool.conninfo
    old = None
    with psycopg.connect(dsn, autocommit=True) as owner:
        owner.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    try:
        files = tmp_path / 'originals'
        files.mkdir()
        old = Store(make_conninfo(dsn, dbname=name), files, 'http://testserver')
        with old.pool.connection() as c:
            c.execute(SOURCE.joinpath('schema.sql').read_text())
            for migration in sorted(SOURCE.joinpath('migrations').glob('*.sql')):
                version = int(migration.name[:3])
                if version > 36:
                    break
                c.execute(migration.read_text())
                if version == 17:
                    from messaging_service.chat_evidence import backfill
                    backfill(c)
                c.execute('INSERT INTO archive_migrations(version,sha256) VALUES(%s,%s)',
                    (version, hashlib.sha256(migration.read_bytes()).hexdigest()))
                c.execute("UPDATE probe_metadata SET value=%s WHERE name='schema_version'", (str(version),))
        old.seed()
        yield old
    finally:
        if old is not None:
            old.close()
        with psycopg.connect(dsn, autocommit=True) as owner:
            owner.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))


# The previous (schema-36) indexer and enrichment templates, verbatim without comments:
# deployed workers keep these grants until the operator reapplies the new templates.
PREVIOUS_INDEXER = '''DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='messaging_indexer') THEN
        CREATE ROLE messaging_indexer NOLOGIN;
    END IF;
END $$;
GRANT USAGE ON SCHEMA public TO messaging_indexer;
GRANT SELECT ON attachments,messages TO messaging_indexer;
GRANT SELECT ON attachment_residency,attachment_eviction_journal TO messaging_indexer;
GRANT SELECT,UPDATE ON attachment_text TO messaging_indexer;
GRANT SELECT,INSERT,UPDATE ON processing_resource_leases TO messaging_indexer;
GRANT SELECT ON message_metadata,message_revisions,events TO messaging_indexer;
GRANT SELECT,UPDATE(value) ON feed_counter TO messaging_indexer;
GRANT INSERT ON events TO messaging_indexer;'''
PREVIOUS_ENRICHER = '''DO $$ BEGIN
 IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='messaging_enricher') THEN
 CREATE ROLE messaging_enricher NOLOGIN;
 END IF;
END $$;
GRANT USAGE ON SCHEMA public TO messaging_enricher;
GRANT SELECT ON attachments,messages,attachment_text TO messaging_enricher;
GRANT SELECT ON attachment_residency,attachment_eviction_journal TO messaging_enricher;
GRANT SELECT,UPDATE ON attachment_enrichment TO messaging_enricher;
GRANT SELECT,INSERT,UPDATE ON enrichment_budget TO messaging_enricher;
GRANT SELECT,INSERT,UPDATE ON processing_profiles TO messaging_enricher;
GRANT SELECT,INSERT,UPDATE ON processing_resource_leases TO messaging_enricher;
GRANT SELECT,INSERT,UPDATE ON processing_budget,processing_control TO messaging_enricher;
GRANT SELECT ON message_metadata,message_revisions,events TO messaging_enricher;
GRANT SELECT,UPDATE(value) ON feed_counter TO messaging_enricher;
GRANT INSERT ON events TO messaging_enricher;'''
PUBLISHERS = ('public.publish_processing_event(text[],text,text,text,text,jsonb)',
              'public.publish_file_text_event(text,text,text,text,jsonb)',
              'public.publish_file_enrichment_event(text,text,text,text,jsonb)')


def previous_append_event(c, account, message, kind='message.created', origin='fixture', event_key=None,
                          snapshot_metadata=None, publisher=None):
    """Store.append_event of the base release (96b72f2), verbatim but for ``publisher``.

    Workers and collectors differ from the base release only in this function and in
    processing_events.publish passing ``publisher``, which the base release did not have;
    with this patched in, every statement they send is the previous release's.
    """
    import json
    from messaging_service.store import serial
    from psycopg.types.json import Jsonb
    row=c.execute('''SELECT id AS message,account,chat,sender,provider_id,body,revision,sent_at,deleted,direction
        FROM messages WHERE id=%s AND account=%s''',(message,account)).fetchone()
    if not row: raise ValueError('invalid_event_message')  # noqa: E701 (verbatim)
    if snapshot_metadata is not None:
        if (not isinstance(snapshot_metadata,dict) or set(snapshot_metadata)-{'processing','reply_evidence'} or
                len(json.dumps(snapshot_metadata,ensure_ascii=False).encode())>16384):
            raise ValueError('invalid_event_metadata')
        if 'reply_evidence' in snapshot_metadata:
            from messaging_service.reply_evidence import validate as validate_reply_evidence
            validate_reply_evidence(snapshot_metadata['reply_evidence'])
        row.update(snapshot_metadata)
    from messaging_service.result_evidence import formatting_evidence
    formatting_evidence(c,[row])
    value = c.execute('UPDATE feed_counter SET value=value+1 WHERE id=1 RETURNING value').fetchone()['value']
    c.execute('''INSERT INTO events(id,account,message,kind,revision,origin,snapshot,event_key)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s)''',
        (value,account,message,kind,row['revision'],origin,Jsonb(serial(row)),event_key))
    return value


def drop_role(store, role):
    with store.pool.connection() as c:
        c.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
        c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


def feed_state(store):
    with store.pool.connection() as c:
        state = {table: c.execute(sql.SQL('SELECT * FROM {} ORDER BY 1,2').format(sql.Identifier(table))).fetchall()
                 for table in ('events', 'consumers', 'deliveries', 'subscriptions', 'subscription_matches',
                               'subscription_consumption', 'feed_counter')}
    # Schema 40 adds retention columns at their pre-retention values (floor 0, no hold).
    for row in state['subscriptions']:
        assert row.pop('retention_floor', 0) == 0 and row.pop('retention_hold', False) is False
        # Schema 62 (ANYM-447): an open definition has no closure time, tombstone or deleted hints.
        assert (row.pop('closed_at', None), row.pop('tombstoned_at', None), row.pop('deleted_hint_counts', {}),
            row.pop('hints_deleted_at', None)) == (None, None, {}, None)
    # Schema 60 (ANYM-522) adds reference columns that explicit definitions leave at their defaults.
    for row in state['subscription_matches']:
        assert (row.pop('account', None), row.pop('chat', None), row.pop('hidden', False),
            row.pop('own_send_reply', False)) == (None, None, False, False)
    return state


# spec: ANYM-254
def test_upgrade_and_rollback_keep_feed_order_and_cursors(schema36_store, monkeypatch):
    from messaging_service.subscriptions import match_subscriptions
    from test_event_delivery import call, emit, identity, subscription
    store = schema36_store
    drain(worker_for(store))
    Archive(store).register('archive-test', 'telegram', 'Synthetic archive', include_chats=['100'])
    reader = store.issue_key('feed-reader', ['telegram-demo'])
    page = store.get_updates(reader, 'main')
    store.ack_updates(reader, 'main', page['receipt'], page['ack_version'])
    token = identity(store)
    sid = subscription(store, token)
    first = emit(store)
    match_subscriptions(store)
    fetched = call(store, token, 'fetch_subscription_events', sid)
    call(store, token, 'ack_subscription_events', sid, fetched['receipt'], fetched['ack_version'])
    # The deployed indexer and collector logins hold the previous templates' grants.
    role = 'anym254_upgrade_' + uuid4().hex[:10]
    collector = 'anym254_collector_' + uuid4().hex[:10]
    with store.pool.connection() as c:
        c.execute(PREVIOUS_INDEXER.replace('messaging_indexer', role))
        c.execute(SOURCE.joinpath('collector_role.sql').read_text().split('-- Schema 37:')[0]
            .replace('messaging_collector', collector))
        migrations = c.execute('SELECT * FROM archive_migrations ORDER BY version').fetchall()
    before = feed_state(store)

    store.migrate()
    store.migrate()
    with store.pool.connection() as c:
        assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value'] == '62'
        assert c.execute('SELECT * FROM archive_migrations WHERE version<=36 ORDER BY version').fetchall() == migrations
    assert feed_state(store) == before
    try:
        worker = worker_for(store)
        worker.connection = lambda: as_role(store, role)
        # Expansion: the new release still publishes with the previous grants before the
        # template is reapplied, by the direct write those grants allow.
        add_file(store, 'file-expansion', 'demo-tg-1', b'expansion readiness')
        assert worker.run_once()['status'] == 'ready'
        expanded = processing_event(store, 'file-expansion', 'file.text.ready')['id']
        assert expanded == first + 1

        # Contraction: after the template the indexer publishes only through the function.
        with store.pool.connection() as c:
            c.execute(SOURCE.joinpath('indexer_role.sql').read_text().replace('messaging_indexer', role))
        refused(store, role, 'UPDATE feed_counter SET value=value+1 WHERE id=1')
        add_file(store, 'file-upgrade', 'demo-tg-1', b'upgrade readiness')
        assert worker.run_once()['status'] == 'ready'
        published = processing_event(store, 'file-upgrade', 'file.text.ready')['id']
        assert published == expanded + 1

        # Rollback of the file worker and collector: the previous release (the base
        # append_event) with the previous grants restored, running on schema 37.
        with store.pool.connection() as c:
            c.execute(PREVIOUS_INDEXER.replace('messaging_indexer', role))
            # The collector grants are unchanged; only the schema-37 PUBLIC revoke is new.
            c.execute(SOURCE.joinpath('collector_role.sql').read_text().split('-- Schema 37:')[0]
                .replace('messaging_collector', collector))
        monkeypatch.setattr(Store, 'append_event', staticmethod(previous_append_event))
        add_file(store, 'file-rollback', 'demo-tg-1', b'rollback readiness')
        assert worker.run_once()['status'] == 'ready'
        rolled_back = processing_event(store, 'file-rollback', 'file.text.ready')['id']
        assert rolled_back == published + 1
        previous = Archive(NS(pool=RolePool(store, collector), append_event=previous_append_event, files=store.files))
        collected = previous.ingest(media_event())
        assert collected['outcome'] == 'created'
        monkeypatch.undo()
        with store.pool.connection() as c:
            ingested = c.execute('SELECT id FROM events WHERE message=%s', (collected['message'],)).fetchone()['id']
        assert ingested == rolled_back + 1
        second = emit(store)
        assert second == ingested + 1
        with store.pool.connection() as c:
            assert [r['id'] for r in c.execute('SELECT id FROM events WHERE id>=%s ORDER BY id', (first,))] == [
                first, expanded, published, rolled_back, ingested, second] == list(range(first, second + 1))
        # The reader's cursor resumes after its acknowledged position, with no gap or repeat.
        assert [i['event_sequence'] for i in store.get_updates(reader, 'main')['items']] == [first, second]
        match_subscriptions(store)
        following = call(store, token, 'fetch_subscription_events', sid)
        assert first not in [e['event_id'] for e in following['items']] and second in [e['event_id'] for e in following['items']]
    finally:
        drop_role(store, role)
        drop_role(store, collector)


def statements(text):
    """Top-level statements of a template, as psql sends them one by one."""
    body = '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('--'))
    parts, current, quoted = [], '', False
    for piece in re.split(r'(\$\$|;)', body):
        if piece == '$$':
            quoted = not quoted
        if piece == ';' and not quoted:
            if current.strip():
                parts.append(current)
            current = ''
        else:
            current += piece
    return parts


# spec: ANYM-254
@pytest.mark.parametrize('template,base,previous', [
    ('indexer_role.sql', 'messaging_indexer', PREVIOUS_INDEXER),
    ('enrichment_role.sql', 'messaging_enricher', PREVIOUS_ENRICHER)], ids=['indexer', 'enricher'])
def test_template_applied_too_early_in_autocommit_keeps_the_previous_grants(schema36_store, template, base, previous):
    store = schema36_store
    role = 'anym254_early_' + uuid4().hex[:10]
    with store.pool.connection() as c:
        c.execute(previous.replace(base, role))
    try:
        errors = []
        # psql without ON_ERROR_STOP: every statement runs, each failure is only reported.
        with psycopg.connect(store.pool.conninfo, autocommit=True) as c:
            for statement in statements(SOURCE.joinpath(template).read_text().replace(base, role)):
                try:
                    c.execute(statement)
                except psycopg.Error as error:
                    errors.append(str(error))
        assert errors and 'schema_37_required' in errors[0]
        with store.pool.connection() as c:
            kept = c.execute('''SELECT has_table_privilege(%(r)s,'events','INSERT') AS insert,
                has_table_privilege(%(r)s,'events','SELECT') AS read,
                has_column_privilege(%(r)s,'feed_counter','value','UPDATE') AS counter''', {'r': role}).fetchone()
        assert kept == {'insert': True, 'read': True, 'counter': True}
    finally:
        drop_role(store, role)


def public_executes(store):
    with store.pool.connection() as c:
        return [c.execute("SELECT has_function_privilege('public',%s,'EXECUTE') AS ok", (f,)).fetchone()['ok']
                for f in PUBLISHERS]


def reopen_to_public(store):
    # A pg_restore --no-acl leaves new functions with the default PUBLIC EXECUTE.
    with store.pool.connection() as c:
        for function in PUBLISHERS:
            c.execute(f'GRANT EXECUTE ON FUNCTION {function} TO PUBLIC')
    assert public_executes(store) == [True, True, True]


# spec: ANYM-254
def test_restored_publishers_are_closed_to_public_by_migrate_and_every_worker_template(archive):  # noqa: F811
    ar, s, _, _ = archive
    reopen_to_public(s)
    s.migrate()
    assert public_executes(s) == [False, False, False]
    roles = []
    try:
        for template, base, account in [('indexer_role.sql', 'messaging_indexer', None),
                                        ('enrichment_role.sql', 'messaging_enricher', None),
                                        ('collector_role.sql', 'messaging_collector', 'archive-test')]:
            reopen_to_public(s)
            roles.append(install(s, template, base, account))
            assert public_executes(s) == [False, False, False]
        indexer, enricher, collector = roles
        mine = ar.ingest(media_event())['message']
        snapshot = {'message': mine, 'account': 'archive-test', 'processing': {'stage': 'text'}}
        for role, function in [(indexer, 'publish_file_enrichment_event'), (enricher, 'publish_file_text_event'),
                               (collector, 'publish_file_text_event'), (collector, 'publish_file_enrichment_event')]:
            with pytest.raises(FORBIDDEN):
                publish(s, role, function, 'archive-test', mine, 'file.text.ready', forged_key(), snapshot)
        for role in roles:
            refused(s, role, '''SELECT public.publish_processing_event(ARRAY['text'],'archive-test',%s,
                'file.text.ready',%s,%s)''', (mine, forged_key(), Jsonb(snapshot)))
    finally:
        for role in roles:
            remove(s, role)


def text_snapshot(s, role):
    drain(worker_for(s))
    worker = worker_for(s)
    worker.connection = lambda: as_role(s, role)
    add_file(s, 'file-values', 'demo-tg-1', b'fenced processing values')
    assert worker.run_once()['status'] == 'ready'
    return published_snapshot(processing_event(s, 'file-values', 'file.text.ready'))


# spec: ANYM-254
def test_processing_fields_carry_only_bounded_values_of_their_type(seeded):
    s, _, _ = seeded
    role = install(s, 'indexer_role.sql', 'messaging_indexer')
    try:
        snapshot = text_snapshot(s, role)
        processing = snapshot['processing']
        with s.pool.connection() as c:
            secret = c.execute("SELECT body FROM messages WHERE id='demo-wa-1'").fetchone()['body']
        without_truncated = {k: v for k, v in processing.items() if k != 'truncated'}
        forged = {
            'nested_model': {**processing, 'model': {'body': secret}},
            'foreign_text_model': {**processing, 'model': secret},
            'long_provider': {**processing, 'provider': 'p' * 129},
            'path_extractor': {**processing, 'extractor': '/home/owner/models/x'},
            'bad_artifact': {**processing, 'artifact_version': 'artifact_' + secret},
            'object_attachment': {**processing, 'attachment_id': {'id': processing['attachment_id']}},
            'text_chars_string': {**processing, 'text_chars': '12'},
            'text_chars_huge': {**processing, 'text_chars': 1_000_001},
            'text_chars_negative': {**processing, 'text_chars': -1},
            'text_chars_fraction': {**processing, 'text_chars': 1.5},
            'truncated_string': {**processing, 'truncated': 'false'},
            'missing_truncated': without_truncated,
            'model_basis_for_text': {**processing, 'basis': 'model_generated'},
            'error_on_ready': {**processing, 'error_code': 'processing_failed'},
            'retryable_on_ready': {**processing, 'retryable': False},
            'other_revision': {**processing, 'source_revision': processing['source_revision'] + 1},
        }
        for name, value in forged.items():
            with pytest.raises(FORBIDDEN):
                publish(s, role, 'publish_file_text_event', 'telegram-demo', 'demo-tg-1', 'file.text.ready',
                        forged_key(), {**snapshot, 'processing': value})
        failed = {**processing, 'state': 'failed', 'error_code': 'retry_exhausted', 'retryable': False,
                  'artifact_version': 'artifact_' + 'c' * 64}
        for name, value in {'free_error': {**failed, 'error_code': secret},
                            'missing_retryable': {k: v for k, v in failed.items() if k != 'retryable'},
                            'string_retryable': {**failed, 'retryable': 'no'}}.items():
            with pytest.raises(FORBIDDEN):
                publish(s, role, 'publish_file_text_event', 'telegram-demo', 'demo-tg-1', 'file.processing.failed',
                        forged_key(), {**snapshot, 'processing': value})
        # The exact shape processing_events.publish produces for a failure is accepted.
        assert publish(s, role, 'publish_file_text_event', 'telegram-demo', 'demo-tg-1', 'file.processing.failed',
                       forged_key(), {**snapshot, 'processing': failed})
        with s.pool.connection() as c:
            assert not c.execute('''SELECT 1 FROM events WHERE account='telegram-demo' AND snapshot::text LIKE %s''',
                ('%' + secret + '%',)).fetchall()
    finally:
        remove(s, role)


def login_conninfo(store, role, password):
    return make_conninfo(store.pool.conninfo, user=role, password=password)


# spec: ANYM-254
def test_bound_login_cannot_write_another_account_by_switching_to_a_shared_role(archive):  # noqa: F811
    ar, s, _, _ = archive
    other, _ = other_account(ar, s)
    mine = ar.ingest(media_event())['message']
    shared = install(s, 'collector_role.sql', 'messaging_collector')
    login, password = 'anym254_login_' + uuid4().hex[:10], uuid4().hex
    with s.pool.connection() as c:
        c.execute(sql.SQL('CREATE ROLE {} LOGIN INHERIT PASSWORD {}').format(sql.Identifier(login), sql.Literal(password)))
        c.execute(sql.SQL('GRANT {} TO {}').format(sql.Identifier(shared), sql.Identifier(login)))
        c.execute('INSERT INTO collector_account_roles(role_name,account) VALUES(%s,%s)', (login, 'archive-test'))
    try:
        for switch in ('', sql.SQL('SET ROLE {}').format(sql.Identifier(shared)),
                       sql.SQL('SET ROLE {}; RESET ROLE').format(sql.Identifier(shared))):
            with psycopg.connect(login_conninfo(s, login, password)) as c:
                if switch:
                    c.execute(switch)
                assert c.execute('SELECT session_user::text AS who').fetchone()[0] == login
                with pytest.raises(FORBIDDEN):
                    c.execute("UPDATE messages SET body='forged' WHERE id=%s", (other,))
                c.rollback()
                if switch:
                    c.execute(switch)
                # Its own account stays writable after the switch.
                c.execute('UPDATE source_status SET status=status WHERE account=%s', ('archive-test',))
        with s.pool.connection() as c:
            assert c.execute('SELECT body FROM messages WHERE id=%s', (other,)).fetchone()['body'] != 'forged'
            assert c.execute('SELECT 1 FROM messages WHERE id=%s', (mine,)).fetchone()
    finally:
        with s.pool.connection() as c:
            c.execute('DELETE FROM collector_account_roles WHERE role_name=%s', (login,))
        drop_role(s, login)
        remove(s, shared)


def publish_in_thread(store, role, args, results):
    try:
        with psycopg.connect(store.pool.conninfo, row_factory=dict_row) as c:
            c.execute(sql.SQL('SET ROLE {}').format(sql.Identifier(role)))
            results.append(c.execute('SELECT public.publish_file_text_event(%s,%s,%s,%s,%s) AS id',
                (*args[:4], Jsonb(args[4]))).fetchone()['id'])
    except psycopg.Error as error:
        results.append(error)


def wait_for_lock_waiters(store, count):
    deadline = time.monotonic() + 20
    with psycopg.connect(store.pool.conninfo, autocommit=True) as c:
        while c.execute("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type='Lock'").fetchone()[0] < count:
            assert time.monotonic() < deadline, 'publication did not wait for the held lock'
            time.sleep(0.05)


ACCOUNT_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended('archive:telegram-demo',0))"


def under_account_lock(store, role, calls, change=None, hold=ACCOUNT_LOCK):
    """Hold a lock (by default the account's ingestion lock) while publications start,
    then change and commit."""
    results = []
    with psycopg.connect(store.pool.conninfo) as holder:
        holder.execute(hold)
        threads = [threading.Thread(target=publish_in_thread, args=(store, role, args, results)) for args in calls]
        for thread in threads:
            thread.start()
        wait_for_lock_waiters(store, len(threads))
        if change:
            holder.execute(change)
        holder.commit()
    for thread in threads:
        thread.join(30)
    return results


# spec: ANYM-254
def test_publication_holds_the_account_lock_against_an_edit_and_a_concurrent_replay(seeded):
    s, _, _ = seeded
    role = install(s, 'indexer_role.sql', 'messaging_indexer')
    try:
        snapshot = text_snapshot(s, role)
        key = forged_key()
        # Two first calls with one key return the same committed event.
        same = under_account_lock(s, role, [('telegram-demo', 'demo-tg-1', 'file.text.ready', key, snapshot)] * 2)
        assert len(same) == 2 and isinstance(same[0], int) and same[0] == same[1]
        # An edit committed while the publication waits makes its snapshot stale: refused.
        stale = under_account_lock(s, role, [('telegram-demo', 'demo-tg-1', 'file.text.ready', forged_key(), snapshot)],
            "UPDATE messages SET body='edited while waiting',revision=revision+1 WHERE id='demo-tg-1'")
        assert len(stale) == 1 and isinstance(stale[0], FORBIDDEN)
        with s.pool.connection() as c:
            assert c.execute('SELECT max(id) AS id FROM events').fetchone()['id'] == same[0]
    finally:
        remove(s, role)


# spec: ANYM-254
def test_publication_refuses_an_attachment_deactivated_while_it_waits(seeded):
    s, _, _ = seeded
    role = install(s, 'indexer_role.sql', 'messaging_indexer')
    try:
        snapshot = text_snapshot(s, role)
        with s.pool.connection() as c:
            newest = c.execute('SELECT max(id) AS id FROM events').fetchone()['id']
        # A direct collector write takes no advisory lock: the publication waits on the row itself.
        waited = under_account_lock(s, role, [('telegram-demo', 'demo-tg-1', 'file.text.ready', forged_key(), snapshot)],
            hold="UPDATE attachments SET active=false WHERE id='file-values'")
        assert len(waited) == 1 and isinstance(waited[0], FORBIDDEN)
        with s.pool.connection() as c:
            assert c.execute('SELECT max(id) AS id,(SELECT value FROM feed_counter) AS counter FROM events').fetchone() == {
                'id': newest, 'counter': newest}
    finally:
        remove(s, role)


# spec: ANYM-519
def test_gateway_role_writes_endpoint_secrets_and_headers_but_never_reads_them(seeded, tmp_path, monkeypatch):
    import base64
    import json
    from copy import copy
    from messaging_service.event_delivery import dispatch_hints
    store, _, _ = seeded
    policy = tmp_path / 'delivery-policy.json'
    policy.write_text(json.dumps({'allow_hosts': ['example.test']}))
    policy.chmod(0o600)
    monkeypatch.setenv('DELIVERY_POLICY_FILE', str(policy))
    public = lambda *a, **k: ['93.184.216.34']  # noqa: E731
    secret = 'whsec_' + base64.b64encode(b'synthetic-endpoint-secret-0123456789').decode()
    permissions = ['read', 'subscription-write', 'delivery-write']
    token = store.issue_key('endpoint-role-agent', ['telegram-demo'], permissions=permissions, key_permissions=permissions)
    store.set_delivery_features(True)
    api_role = install(store, 'gateway_role.sql', 'messaging_gateway')
    dispatcher_role = install(store, 'delivery_role.sql', 'messaging_event_delivery')
    member = 'anym519_member_' + uuid4().hex[:10]
    try:
        api, worker = copy(store), copy(store)
        api.pool, worker.pool = RolePool(store, api_role), RolePool(store, dispatcher_role)
        # The API's real calls succeed with write-only columns.
        created = api.register_delivery_endpoint(token, 'endpoint-request-1', 'https://hooks.example.test/hook', secret,
            headers={'X-Auth': 'synthetic-auth'}, resolver=public)
        eid = created['endpoint_id']
        assert [item['endpoint_id'] for item in api.list_delivery_endpoints(token)['items']] == [eid]
        api.update_delivery_endpoint(token, eid, 1, 'set_headers', headers={'X-Auth': 'synthetic-auth-2'})
        api.update_delivery_endpoint(token, eid, 2, 'rotate_secret', new_secret='whsec_' + base64.b64encode(b'r' * 32).decode())
        api.update_delivery_endpoint(token, eid, 3, 'pause')
        with store.pool.connection() as c:
            c.execute("UPDATE delivery_endpoint_tests SET created_at=created_at-interval '2 minutes' WHERE endpoint=%s", (eid,))
        # ANYM-520: the resume stages its signed test; once it is delivered the API role re-arms the endpoint itself.
        assert api.update_delivery_endpoint(token, eid, 4, 'resume')['test']['test_status'] == 'pending'
        with store.pool.connection() as c:
            c.execute('''UPDATE delivery_endpoint_tests SET state='delivered',finished_at=clock_timestamp(),
                created_at=created_at-interval '2 minutes' WHERE endpoint=%s AND starts_with(request_id,'resume/')''', (eid,))
        assert api.update_delivery_endpoint(token, eid, 4, 'resume')['state'] == 'enabled'
        assert api.test_delivery_endpoint(token, eid, 'endpoint-test-1')['test_status'] == 'pending'
        # Only the dispatcher reads them: it signs and sends the test with the caller's header.
        sent = []
        dispatch_hints(worker, transport=lambda url, body, headers, **k: sent.append(headers) or 204, resolver=public)
        assert sent and sent[0]['X-Auth'] == 'synthetic-auth-2' and len(sent[0]['webhook-signature'].split(' ')) == 2
        for column in ('signing_secret', 'pending_secret', 'header_values'):
            refused(store, api_role, 'SELECT ' + column + ' FROM delivery_endpoints')
            refused(store, api_role, 'UPDATE delivery_endpoints SET label=' + column + ' WHERE id=%s', (eid,))
            refused(store, api_role, 'UPDATE delivery_endpoints SET state=state WHERE id=%s RETURNING ' + column, (eid,))
            refused(store, api_role, '''INSERT INTO delivery_endpoints(principal,key_id,request_id,request_hash,url,host,
                signing_secret) VALUES('x','00000000-0000-4000-8000-000000000000','synthetic-1',%s,'u','h','s')
                RETURNING ''' + column, ('a' * 64,))
        refused(store, api_role, 'SELECT * FROM delivery_endpoints')
        # An inherited grant reads no more than the role it inherits.
        with store.pool.connection() as c:
            c.execute(sql.SQL('CREATE ROLE {} NOLOGIN IN ROLE {}').format(sql.Identifier(member), sql.Identifier(api_role)))
        refused(store, member, 'SELECT signing_secret FROM delivery_endpoints')
        refused(store, member, 'SELECT header_values FROM delivery_endpoints')
        assert api.delete_delivery_endpoint(token, eid, 5)['deleted'] is True
        with store.pool.connection() as c:
            assert c.execute('SELECT signing_secret,pending_secret,header_values FROM delivery_endpoints WHERE id=%s',
                (eid,)).fetchone() == {'signing_secret': None, 'pending_secret': None, 'header_values': None}
    finally:
        store.set_delivery_features(False)
        with store.pool.connection() as c:
            c.execute(sql.SQL('DROP ROLE IF EXISTS {}').format(sql.Identifier(member)))
        remove(store, api_role)
        remove(store, dispatcher_role)


# spec: ANYM-519
def test_hint_references_ignore_the_dispatchers_temporary_objects(seeded):
    """The definer function reads the real tables even when the caller shadows them with temporary ones."""
    store, _, _ = seeded
    dispatcher_role = install(store, 'delivery_role.sql', 'messaging_event_delivery')
    subscription = str(uuid4())
    try:
        with as_role(store, dispatcher_role) as c:
            c.execute('''CREATE TEMP TABLE subscription_matches(subscription uuid, event bigint)''')
            c.execute('''CREATE TEMP TABLE events(id bigint, kind text, origin text, account text,
                created_at timestamptz, snapshot jsonb)''')
            c.execute('INSERT INTO subscription_matches VALUES(%s,1)', (subscription,))
            c.execute('''INSERT INTO events VALUES(1,'message.created','live','telegram-demo',clock_timestamp(),
                '{"chat":"synthetic-forged-chat"}')''')
            rows = c.execute('SELECT * FROM public.subscription_hint_references(%s,0,10,%s,NULL)',
                (subscription, ['telegram-demo'])).fetchall()
        assert rows == []
    finally:
        remove(store, dispatcher_role)
