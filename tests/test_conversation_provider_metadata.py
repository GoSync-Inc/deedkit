"""Provider classification uses cached synthetic evidence, never text guesses."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from messaging_service.adapters import telegram_message, whatsapp_message
from messaging_service.envelope import fingerprint, validate


def telegram(**changes):
    values = dict(chat_id=42, id=19, sender_id=42, message='',
        date=datetime(2026, 10, 1, tzinfo=timezone.utc), edit_date=None,
        out=False, file=None, document=None, photo=None)
    values.update(changes)
    return SimpleNamespace(**values)


def whatsapp(**changes):
    values = dict(messageId='false_42@c.us_SYNTHETIC', chatId='42@c.us',
        senderId='42@c.us', timestamp=1791000000, body='', hasMedia=False)
    values.update(changes)
    return values


def test_service_account_text_is_a_message_not_a_system_event():
    result = telegram_message('tg-fixture', telegram(chat_id=777000, message='Login notice'))
    assert result['metadata']['is_service'] is False
    assert result['metadata']['message_kind'] == 'text'
    assert result['metadata']['timestamp_resolution'] == 'second'


def test_observed_telegram_service_action_is_classified_without_empty_text_guessing():
    result = telegram_message('tg-fixture', telegram(action=SimpleNamespace()))
    assert result['metadata']['message_kind'] == 'system'
    assert result['metadata']['provider_message_type'] == 'service'
    assert result['metadata']['is_service'] is True


@pytest.mark.parametrize('media_kind,type_name', [
    ('location', 'MessageMediaGeo'), ('contact', 'MessageMediaContact'),
    ('poll', 'MessageMediaPoll'), ('unknown', 'MessageMediaUnsupported'),
])
def test_telegram_non_file_media_types_are_observed(media_kind, type_name):
    from telethon import types
    # The adapter reads only the constructor type, not geolocation/contact contents.
    result = telegram_message('tg-fixture', telegram(media=object.__new__(getattr(types, type_name))))
    assert result['metadata']['message_kind'] == media_kind


def test_telegram_voice_duration_is_observed_without_changing_body_or_revision_hash():
    result = telegram_message('tg-fixture', telegram(message='caption', voice=True,
        file=SimpleNamespace(name='voice.ogg', ext='.ogg', mime_type='audio/ogg',
            size=10, duration=12.5), document=SimpleNamespace(id=200)))
    assert result['body'] == 'caption'
    assert result['metadata']['message_kind'] == 'voice'
    assert result['metadata']['media_duration_seconds'] == 12.5
    assert fingerprint(result) == fingerprint({**result, 'metadata':{}})


@pytest.mark.parametrize('native,kind', [
    ('chat', 'text'), ('ptt', 'voice'), ('location', 'location'),
    ('vcard', 'contact'), ('multi_vcard', 'contact'), ('poll_creation', 'poll'),
    ('notification', 'system'), ('future_unsupported', 'unknown'),
])
def test_whatsapp_native_type_is_classified_independently_of_text(native, kind):
    result = whatsapp_message('wa-fixture', whatsapp(messageType=native, body='Caption'))
    assert result['metadata']['message_kind'] == kind
    assert result['metadata']['timestamp_resolution'] == 'second'
    assert result['metadata']['provider_message_type'] == native


# spec: ANYM-471
def test_whatsapp_business_messages_with_text_are_text_and_masked_ones_stay_unknown():
    for native in ('template', 'interactive', 'buttons', 'list', 'product', 'group_invite'):
        result = whatsapp_message('wa-fixture', whatsapp(messageType=native, body='Invoice 42'))
        assert result['metadata']['message_kind'] == 'text'
        assert result['metadata']['provider_message_type'] == native
        assert whatsapp_message('wa-fixture', whatsapp(messageType=native))['metadata']['message_kind'] == 'unknown'
    placeholder = whatsapp_message('wa-fixture', whatsapp(messageType='placeholder'))
    assert placeholder['metadata']['message_kind'] == 'unknown' and placeholder['body'] == ''
    for native in ('album', 'history_notice'):
        service = whatsapp_message('wa-fixture', whatsapp(messageType=native, isService=True))
        assert service['metadata']['message_kind'] == 'system'


def test_whatsapp_missing_type_and_empty_body_stays_unknown():
    result = whatsapp_message('wa-fixture', whatsapp())
    assert result['metadata']['message_kind'] == 'unknown'
    assert result['metadata']['provider_message_type'] is None
    assert result['metadata'].get('media_duration_seconds') is None


def test_whatsapp_voice_preserves_observed_duration():
    result = whatsapp_message('wa-fixture', whatsapp(messageType='ptt',
        mediaKind='voice', durationSeconds=12, hasMedia=True, mimetype='audio/ogg'))
    assert result['metadata']['media_duration_seconds'] == 12
    assert result['body'] == ''


@pytest.mark.parametrize('key,value', [
    ('message_kind', 'invented'), ('provider_message_type', 'provider raw secret\n'),
    ('timestamp_resolution', 'microsecond'), ('media_duration_seconds', True),
    ('media_duration_seconds', -1), ('media_duration_seconds', 86401),
    ('media_duration_seconds', 10**1000),
])
def test_provider_metadata_contract_rejects_unreviewed_values(key, value):
    result = telegram_message('tg-fixture', telegram(message='text'))
    result['metadata'][key] = value
    with pytest.raises(ValueError, match='invalid_metadata'):
        validate(result)
