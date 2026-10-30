"""Independent regressions for source evidence surviving MCP compact rendering."""
from messaging_service.conversation import present_message, presentation
from messaging_service.presentation import message_summary
from test_conversation_state import dialogue_archive as dialogue_archive


def test_compact_conversation_keeps_provider_reference_and_precision_provenance():
    row=present_message({'id':'synthetic-message','provider_id':'101','body':'Synthetic text',
        'direction':'incoming','timestamp_resolution':'second',
        'first_observed_at':'2026-10-15T00:00:00+00:00',
        'first_observed_at_basis':'archive_revision_observation',
        'ordering':{'basis':'provider_message_id','status':'known',
            'phone_order_verified':False}},1)
    result=message_summary(row)
    assert result.get('provider_id')=='101'
    assert result.get('timestamp_precision')=='second'
    assert result.get('first_observed_at_basis')=='archive_revision_observation'
    assert result.get('ordering')==row['ordering']


def test_compact_conversation_does_not_erase_readable_text_truncation():
    row=present_message({'id':'synthetic-message','body':'Full original body',
        'direction':'outgoing'},1)
    row.update(display_text='Full…',display_text_truncated=True)
    result=message_summary(row)
    assert result.get('body')=='Full original body'
    assert result.get('display_text_truncated') is True


def test_multichat_transcript_identifies_each_account_and_chat():
    rows=[present_message({'id':'synthetic-one','body':'First conversation',
        'account':'synthetic-account-one','chat':'synthetic-chat-one',
        'chat_title':'Synthetic project one','sender_name':'Synthetic sender',
        'direction':'incoming','sent_at':'2026-10-15T00:00:00+00:00'},1),
        present_message({'id':'synthetic-two','body':'Second conversation',
        'account':'synthetic-account-two','chat':'synthetic-chat-two',
        'chat_title':'Synthetic project two','sender_name':'Synthetic sender',
        'direction':'outgoing','sent_at':'2026-10-15T00:00:01+00:00'},2)]
    transcript=presentation(rows)['transcript']
    lines=transcript.split('\n\n')
    for row,line in zip(rows,lines):
        assert row['account'] in line
        assert row['chat'] in line
        assert row['chat_title'] in line
        assert row['display_text'] in line


def test_unclassified_whatsapp_sibling_cannot_report_known_head_order():
    from datetime import datetime, timezone
    from messaging_service.conversation_state import derive_state
    at=datetime(2026,10,15,tzinfo=timezone.utc)
    def message(mid,body,direction,kind):
        return {'id':mid,'provider_id':mid,'revision':1,'body':body,
            'display_text':body or '[Сообщение неизвестного типа]',
            'sent_at':at,'direction':direction,'kind':kind,'is_service':False,
            'order_basis':'timestamp_only','order_status':'ambiguous'}
    result=derive_state([message('known','Synthetic text','incoming','text'),
        message('unknown','','outgoing','unknown')],platform='whatsapp',
        chat_type='private',as_of=at,history={'last_head_scan_at':at,'gaps':[]})
    assert result['last_turn']=='unknown'
    assert result['suggested_action']=='unclear'
    assert result['order_status']=='ambiguous'


def test_legacy_animation_metadata_keeps_observed_media_kind():
    row=present_message({'id':'synthetic-animation','body':'Synthetic caption',
        'direction':'incoming','media_kind':'animation'},1)
    assert row['kind']=='video'
    assert row['body']=='Synthetic caption'
    assert '[Видео]' in row['display_text']


def test_context_cursor_cannot_adopt_a_backfill_missing_from_its_window(dialogue_archive,monkeypatch):
    import pytest
    from messaging_service import message_queries
    from messaging_service.envelope import stable_id
    from messaging_service.store import APIError
    store,key,add,_,_=dialogue_archive
    chat=add('old',second=-10)
    add('anchor',second=0)
    add('new',second=10)
    anchor_id=stable_id('msg','dialogue-wa','123@g.us','anchor')
    original=message_queries.history_cursor
    injected=False
    def insert_before_first_cursor(*args,**kwargs):
        nonlocal injected
        if not injected:
            injected=True
            add('late-middle',second=-5)
        return original(*args,**kwargs)
    monkeypatch.setattr(message_queries,'history_cursor',insert_before_first_cursor)
    try:
        result=store.get_context(key,anchor_id,view='conversation',
            before_count=1,after_count=1,include_coverage=False)
    except APIError as error:
        assert error.code=='cursor_changed'
        return
    assert injected
    window=result['context_window']
    # If the context is pinned to an older snapshot, its continuation must reject
    # a newer view. It must not silently skip a row inserted inside the window.
    with pytest.raises(APIError,match='cursor_changed'):
        store.get_messages(key,chat,view='conversation',order=window['older_order'],
            before=window['older_cursor'],include_coverage=False)
