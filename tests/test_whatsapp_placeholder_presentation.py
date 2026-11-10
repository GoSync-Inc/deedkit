"""Synthetic linked-device placeholders retain their source and explain phone-only content."""
import pytest
from psycopg.types.json import Jsonb

from messaging_service.conversation import present_message
from messaging_service.presentation import lean_message, message_summary


LABEL = '[Содержимое недоступно на связанном устройстве WhatsApp. Откройте сообщение на основном телефоне]'


# spec: ANYM-471
@pytest.mark.parametrize('provider_type', ['placeholder', 'biz_content_placeholder'])
def test_masked_whatsapp_content_explains_where_to_read_it(provider_type):
    source = {'id': 'synthetic', 'platform': 'whatsapp', 'provider_message_type': provider_type,
        'message_kind': 'unknown', 'body': '', 'attachments': []}
    row = present_message(dict(source), 1)
    assert row['display_text'] == LABEL
    assert row['body'] == '' and row['kind'] == 'unknown'
    assert row['provider_message_type'] == provider_type and row['attachments'] == []
    assert lean_message(row)['display_text'] == LABEL
    assert message_summary(row)['display_text'] == LABEL


# spec: ANYM-471
@pytest.mark.parametrize('changes', [
    {'platform': 'telegram'}, {'platform': None}, {'provider_message_type': 'future_type'},
    {'body': 'Synthetic content received later'}, {'attachments': [{'filename': 'synthetic.txt'}]},
    {'deleted': True}, {'message_kind': 'photo'}, {'is_service': True},
])
def test_phone_only_label_requires_an_empty_live_whatsapp_placeholder(changes):
    source = {'id': 'synthetic', 'platform': 'whatsapp', 'provider_message_type': 'placeholder',
        'message_kind': 'unknown', 'body': '', 'attachments': []}
    source.update(changes)
    row = present_message(dict(source), 1)
    assert LABEL not in row['display_text']
    assert row['body'] == source['body'] and row['attachments'] == source['attachments']


# spec: ANYM-471
@pytest.mark.parametrize('provider_type', ['placeholder', 'biz_content_placeholder'])
@pytest.mark.parametrize('snapshot_only', [False, True])
def test_archived_placeholder_is_explained_in_message_context_and_chat_views(seeded, provider_type, snapshot_only):
    store, token, _ = seeded
    metadata = {'message_kind': 'unknown', 'provider_message_type': provider_type,
        'timestamp_resolution': 'second'}
    with store.pool.connection() as c:
        c.execute("""INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction)
            VALUES('masked','whatsapp-demo-1','whatsapp-demo-1-chat','synthetic-masked',
            'synthetic-peer','','2026-12-01T12:00:00Z','incoming')""")
        c.execute("""INSERT INTO message_revisions(message,revision,snapshot)
            VALUES('masked',1,%s)""", (Jsonb({'metadata': metadata}),))
        if not snapshot_only:
            c.execute("INSERT INTO message_metadata(message,revision,metadata) VALUES('masked',1,%s)",
                (Jsonb(metadata),))
    messages = store.get_messages(token, 'whatsapp-demo-1-chat', view='conversation',
        from_time='2026-12-01T00:00:00Z')
    context = store.get_context(token, 'masked', view='conversation', before_count=0, after_count=0)
    search = store.search(token, chat='whatsapp-demo-1-chat', view='conversation',
        from_time='2026-12-01T00:00:00Z')
    for result, rows in [(messages, messages['items']), (context, context['context']), (search, search['items'])]:
        assert rows[0]['display_text'] == LABEL
        assert rows[0]['kind'] == 'unknown' and rows[0]['body'] == ''
        assert LABEL in result['conversation_view']['transcript']
    assert context['message']['display_text'] == LABEL


# spec: ANYM-471
def test_later_text_replaces_the_placeholder_label(seeded):
    store, token, _ = seeded
    with store.pool.connection() as c:
        c.execute("""INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction)
            VALUES('completed','whatsapp-demo-1','whatsapp-demo-1-chat','synthetic-completed',
            'synthetic-peer','','2026-12-01T12:00:00Z','incoming')""")
        c.execute("INSERT INTO message_metadata(message,revision,metadata) VALUES('completed',1,%s)",
            (Jsonb({'message_kind': 'unknown', 'provider_message_type': 'placeholder'}),))
    first = store.get_context(token, 'completed', view='conversation', before_count=0, after_count=0)
    assert first['message']['display_text'] == LABEL
    with store.pool.connection() as c:
        c.execute("UPDATE messages SET body='Synthetic delivered text',revision=2 WHERE id='completed'")
        c.execute("UPDATE message_metadata SET revision=2,metadata=%s WHERE message='completed'",
            (Jsonb({'message_kind': 'text', 'provider_message_type': 'template'}),))
    updated = store.get_context(token, 'completed', view='conversation', before_count=0, after_count=0)
    assert updated['message']['display_text'] == 'Synthetic delivered text'
    assert updated['message']['kind'] == 'text' and updated['message']['revision'] == 2
