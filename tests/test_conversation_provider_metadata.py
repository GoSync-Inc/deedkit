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


# spec: ANYM-922
def test_a_shared_telegram_point_or_venue_keeps_its_coordinates_title_and_address():
    from telethon import types
    geo = types.GeoPoint(long=37.61729999, lat=55.75582612, access_hash=1, accuracy_radius=15)
    point = telegram_message('tg-fixture', telegram(media=types.MessageMediaGeo(geo=geo)))
    assert point['metadata']['message_kind'] == 'location'
    assert point['metadata']['location'] == {'latitude': 55.755826, 'longitude': 37.6173, 'accuracy_m': 15,
        'title': None, 'address': None, 'live': False}
    venue = telegram_message('tg-fixture', telegram(media=types.MessageMediaVenue(
        geo=types.GeoPoint(long=-0.1276, lat=51.5072, access_hash=1), title='Кафе\x00 «Встреча»' + 'я' * 300,
        address='Main St 1', provider='foursquare', venue_id='synthetic', venue_type='')))
    location = venue['metadata']['location']
    assert (location['latitude'], location['longitude'], location['accuracy_m'], location['address'], location['live']) == \
        (51.5072, -0.1276, None, 'Main St 1', False)
    assert location['title'].startswith('Кафе  «Встреча»') and len(location['title']) == 256
    live = telegram_message('tg-fixture', telegram(media=types.MessageMediaGeoLive(geo=geo, period=900)))
    assert live['metadata']['message_kind'] == 'location' and live['metadata']['location'] == {'live': True}
    # No point stated: the kind stays, no block is guessed.
    empty = telegram_message('tg-fixture', telegram(media=types.MessageMediaGeo(geo=types.GeoPointEmpty())))
    assert empty['metadata']['message_kind'] == 'location' and 'location' not in empty['metadata']
    # A deleted message keeps no place.
    assert 'location' not in validate({**point, 'deleted': True})['metadata']


# spec: ANYM-922
def test_a_whatsapp_location_event_keeps_the_same_block_and_a_live_one_only_the_fact():
    block = {'latitude': 55.7558, 'longitude': 37.6173, 'accuracy_m': None, 'title': 'Парк', 'address': 'ул. Синтетическая 1',
        'live': False}
    point = whatsapp_message('wa-fixture', whatsapp(messageType='location', body='Парк', location=block))
    assert point['metadata']['message_kind'] == 'location' and point['metadata']['location'] == block
    live = whatsapp_message('wa-fixture', whatsapp(messageType='location', location={'live': True}))
    assert live['metadata']['location'] == {'live': True}
    # Only a location message carries a place, and a delete keeps none.
    assert 'location' not in whatsapp_message('wa-fixture', whatsapp(messageType='chat', body='x', location=block))['metadata']
    gone = whatsapp_message('wa-fixture', whatsapp(messageType='location', location=block), change='delete',
        observed_at=1791000100)
    assert 'location' not in gone['metadata']


# spec: ANYM-922
@pytest.mark.parametrize('block', [
    {'latitude': 91, 'longitude': 0, 'accuracy_m': None, 'title': None, 'address': None, 'live': False},
    {'latitude': 0, 'longitude': -180.5, 'accuracy_m': None, 'title': None, 'address': None, 'live': False},
    {'latitude': float('nan'), 'longitude': 0, 'accuracy_m': None, 'title': None, 'address': None, 'live': False},
    {'latitude': 0, 'longitude': float('inf'), 'accuracy_m': None, 'title': None, 'address': None, 'live': False},
    {'latitude': True, 'longitude': 0, 'accuracy_m': None, 'title': None, 'address': None, 'live': False},
    {'latitude': '55.7', 'longitude': 0, 'accuracy_m': None, 'title': None, 'address': None, 'live': False},
    {'latitude': 1, 'longitude': 1, 'accuracy_m': -1, 'title': None, 'address': None, 'live': False},
    {'latitude': 1, 'longitude': 1, 'accuracy_m': None, 'title': 'x' * 257, 'address': None, 'live': False},
    {'latitude': 1, 'longitude': 1, 'accuracy_m': None, 'title': None, 'address': 'a\x00b', 'live': False},
    {'latitude': 1, 'longitude': 1, 'accuracy_m': None, 'title': None, 'address': None, 'live': False, 'url': 'x'},
    {'latitude': 1, 'longitude': 1, 'live': True},
    {'longitude': 1, 'live': False},
    {'live': 'yes'},
    [55.7, 37.6],
])
def test_an_invalid_location_block_is_dropped_and_the_message_is_still_stored(block, caplog):
    for result in (whatsapp_message('wa-fixture', whatsapp(messageType='location', body='Парк', location=block)),
                   validate({**whatsapp_message('wa-fixture', whatsapp(messageType='location', body='Парк')),
                       'metadata': {'message_kind': 'location', 'location': block}})):
        assert result['body'] == 'Парк' and result['metadata']['message_kind'] == 'location'
        assert 'location' not in result['metadata']
    assert 'invalid_location' in caplog.text and '55.7' not in caplog.text


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


# spec: ANYM-471
def test_whatsapp_bot_menu_replies_are_text_with_the_chosen_option():
    for native in ('list_response', 'buttons_response', 'template_reply', 'interactive_response'):
        result = whatsapp_message('wa-fixture', whatsapp(messageType=native, body='Chose menu option: Soup [id: row_soup]'))
        assert result['metadata']['message_kind'] == 'text'
        assert result['metadata']['provider_message_type'] == native
        assert result['body'] == 'Chose menu option: Soup [id: row_soup]'
        assert whatsapp_message('wa-fixture', whatsapp(messageType=native))['metadata']['message_kind'] == 'unknown'


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
