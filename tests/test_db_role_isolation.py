"""Narrow collector, indexer and enrichment database roles (ANYM-254)."""
from contextlib import contextmanager
import hashlib
from pathlib import Path
from types import SimpleNamespace as NS
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
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


# Grants of the previous indexer template: a rolled-back schema-36 release publishes directly.
PREVIOUS_INDEXER = '''GRANT USAGE ON SCHEMA public TO {role};
GRANT SELECT ON attachments,messages,message_metadata,message_revisions,events TO {role};
GRANT SELECT,UPDATE(value) ON feed_counter TO {role};
GRANT INSERT ON events TO {role};'''


def feed_state(store):
    with store.pool.connection() as c:
        return {table: c.execute(sql.SQL('SELECT * FROM {} ORDER BY 1,2').format(sql.Identifier(table))).fetchall()
                for table in ('events', 'consumers', 'deliveries', 'subscriptions', 'subscription_matches',
                              'subscription_consumption', 'feed_counter')}


# spec: ANYM-254
def test_upgrade_and_rollback_keep_feed_order_and_cursors(schema36_store):
    from messaging_service.subscriptions import match_subscriptions
    from test_event_delivery import call, emit, identity, subscription
    store = schema36_store
    drain(worker_for(store))
    reader = store.issue_key('feed-reader', ['telegram-demo'])
    page = store.get_updates(reader, 'main')
    store.ack_updates(reader, 'main', page['receipt'], page['ack_version'])
    token = identity(store)
    sid = subscription(store, token)
    first = emit(store)
    match_subscriptions(store)
    fetched = call(store, token, 'fetch_subscription_events', sid)
    call(store, token, 'ack_subscription_events', sid, fetched['receipt'], fetched['ack_version'])
    with store.pool.connection() as c:
        migrations = c.execute('SELECT * FROM archive_migrations ORDER BY version').fetchall()
    before = feed_state(store)

    store.migrate()
    store.migrate()
    with store.pool.connection() as c:
        assert c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value'] == '37'
        assert c.execute('SELECT * FROM archive_migrations WHERE version<=36 ORDER BY version').fetchall() == migrations
    assert feed_state(store) == before

    # The new release: the indexer publishes through the function, after every committed event.
    role = 'anym254_upgrade_' + uuid4().hex[:10]
    with store.pool.connection() as c:
        c.execute(SOURCE.joinpath('indexer_role.sql').read_text().replace('messaging_indexer', role))
    try:
        worker = worker_for(store)
        worker.connection = lambda: as_role(store, role)
        add_file(store, 'file-upgrade', 'demo-tg-1', b'upgrade readiness')
        assert worker.run_once()['status'] == 'ready'
        published = processing_event(store, 'file-upgrade', 'file.text.ready')['id']
        assert published == first + 1

        # Rollback: the previous release with its grants restored publishes directly on schema 37.
        with store.pool.connection() as c:
            c.execute(PREVIOUS_INDEXER.format(role=role))
        with as_role(store, role) as c:
            rolled_back = store.append_event(c, 'telegram-demo', 'demo-tg-1', 'file.text.ready', 'live',
                'processing_' + 'b' * 64, snapshot_metadata={'processing': {'stage': 'text'}})
        assert rolled_back == published + 1
        second = emit(store)
        assert second == rolled_back + 1
        with store.pool.connection() as c:
            assert [r['id'] for r in c.execute('SELECT id FROM events WHERE id>=%s ORDER BY id', (first,))] == [
                first, published, rolled_back, second] == list(range(first, second + 1))
        # The reader's cursor resumes after its acknowledged position, with no gap or repeat.
        assert [i['event_sequence'] for i in store.get_updates(reader, 'main')['items']] == [first, second]
        match_subscriptions(store)
        following = call(store, token, 'fetch_subscription_events', sid)
        assert first not in [e['event_id'] for e in following['items']] and second in [e['event_id'] for e in following['items']]
    finally:
        with store.pool.connection() as c:
            c.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
            c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))

