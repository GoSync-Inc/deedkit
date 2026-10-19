"""The voice model learns the counterparty's local time when the call starts, not when the brief was saved."""
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from core import AgentBrief, BriefInput, VoiceService
from outbound import AgentExternalBrief
from voice import create_model

AT = datetime(2026, 10, 19, 9, 22, tzinfo=timezone.utc)


def instruction(tmp_path, at=AT, **fields):
    store = VoiceService(tmp_path)
    brief = store.prepare(BriefInput(goal='Delivery', counterparty='Shop', success_fields=['window'], **fields), [])
    store.close()
    return brief, create_model(brief, 'fixture', at=at)._settings.system_instruction


# spec: ANYM-239
@pytest.mark.parametrize('engine', ['gemini_live', 'grok_realtime', 'gemini_live_flows'])
def test_call_start_gives_the_model_local_time_and_the_matching_greeting(tmp_path, engine):
    # Regression (Grok feedback 5074d978, 2026-10-19): an Indonesian call greeted "selamat pagi" at 17:22 local.
    brief, text = instruction(tmp_path, language='id', timezone='Asia/Makassar', dialog_engine=engine)
    assert text.startswith(brief['prompt'])
    assert 'Counterparty local time at call start: Monday 2026-10-19 17:22 (Asia/Makassar).' in text
    assert "a time-of-day greeting must be 'Selamat sore'" in text
    assert 'pagi' not in text.casefold()
    assert 'local time' not in brief['prompt']


# spec: ANYM-239
@pytest.mark.parametrize('language,zone,hour,greeting', [
    ('id', 'Asia/Jakarta', 22, 'Selamat pagi'), ('id', 'Asia/Jakarta', 4, 'Selamat siang'),
    ('id', 'Asia/Jakarta', 11, 'Selamat malam'),
    ('ru', 'Europe/Moscow', 4, 'Доброе утро'), ('ru', 'Europe/Moscow', 10, 'Добрый день'),
    ('ru', 'Europe/Moscow', 16, 'Добрый вечер'),
    ('en', 'Europe/London', 6, 'Good morning'), ('en', 'Europe/London', 12, 'Good afternoon'),
    ('en', 'Europe/London', 18, 'Good evening'),
])
def test_greeting_follows_the_local_hour_in_the_call_language(tmp_path, language, zone, hour, greeting):
    _, text = instruction(tmp_path, at=AT.replace(hour=hour), language=language, timezone=zone)
    assert f"a time-of-day greeting must be '{greeting}'" in text


# spec: ANYM-239
@pytest.mark.parametrize('language,neutral', [('id', 'Halo'), ('ru', 'Здравствуйте'), ('en', 'Hello')])
def test_unknown_or_late_local_time_gets_a_neutral_greeting(tmp_path, language, neutral):
    _, text = instruction(tmp_path, language=language)
    assert 'local time is unknown' in text and f"greet with '{neutral}'" in text
    assert 'time-of-day greeting must be' not in text
    _, text = instruction(tmp_path, at=AT.replace(hour=19), language=language, timezone='Asia/Jakarta')
    assert '02:22' in text and f"greet with '{neutral}'" in text


# spec: ANYM-239
@pytest.mark.parametrize('value', ['Mars/Base', 'localtime', 'posixrules', '../../etc/passwd', 'asia/makassar', ''])
def test_brief_timezone_must_be_an_iana_zone(value):
    with pytest.raises(ValidationError):
        AgentBrief(goal='Delivery', counterparty='Shop', success_fields=['window'], timezone=value)
    with pytest.raises(ValidationError):
        AgentExternalBrief(goal='Delivery', counterparty='Shop', success_fields=['window'], timezone=value,
                           channel='phone', account='twilio', destination='+12025550123')
    assert AgentBrief(goal='Delivery', counterparty='Shop', success_fields=['window'], timezone='UTC').timezone == 'UTC'


# spec: ANYM-239
def test_a_zone_the_host_cannot_load_still_starts_the_call_with_a_neutral_greeting(tmp_path, monkeypatch):
    import core
    def missing(key):
        raise core.ZoneInfoNotFoundError(key)
    monkeypatch.setattr(core, 'ZoneInfo', missing)
    _, text = instruction(tmp_path, language='id', timezone='Asia/Makassar')
    assert 'local time is unknown' in text and "greet with 'Halo'" in text
