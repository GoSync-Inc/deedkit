"""ANYM-892: fixed ru/en scheduling rules (scheduling_rules.signals), checked against synthetic fixtures."""
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from zoneinfo import ZoneInfo, available_timezones

import pytest
from hypothesis import given, settings, strategies as st

from messaging_service import scheduling_rules, time_references
from messaging_service.scheduling_rules import MAX_CHARS, signals
from messaging_service.time_references import owner_zone

FIXTURES = Path(__file__).parent / 'fixtures' / 'scheduling'
PHRASES = json.loads((FIXTURES / 'phrases.json').read_text(encoding='utf-8'))
NEGATIVES = json.loads((FIXTURES / 'negatives.json').read_text(encoding='utf-8'))
ZONES = available_timezones() | {'UTC'}
LISBON = owner_zone({'ANYMSG_OWNER_TIMEZONE': 'Europe/Lisbon'})
MONDAY = datetime(2026, 11, 9, 10, tzinfo=timezone.utc)


def run(row):
    zone = owner_zone({'ANYMSG_OWNER_TIMEZONE': row['owner_zone']} if row['owner_zone'] else {})
    return signals(row['text'], datetime.fromisoformat(row['sent_at']), row['direction'], zone)


def observed(row, got):
    """The result in the fixture's shape: the cue and each time's phrase as the text they span."""
    text = row['text']
    times = [{'phrase': text[slice(*t['span'])], **{k: t[k] for k in ('start', 'end', 'granularity', 'zone', 'zone_source',
        'assumed', 'polarity')}} for t in got['times']]
    return {'intent': got['intent'], 'medium': got['medium'], 'cue': text[slice(*got['cue_span'])] if got['cue_span'] else None,
        'times': times}


# spec: ANYM-892
def test_phrase_fixture_has_the_required_rows_and_fields():
    langs = [row['id'].split('-')[0] for row in PHRASES]
    assert len(PHRASES) >= 120 and langs.count('ru') >= 60 and langs.count('en') >= 40 and langs.count('mixed') >= 20
    assert len({row['id'] for row in PHRASES}) == len(PHRASES)
    for row in PHRASES:
        assert {'text', 'sent_at', 'direction', 'owner_zone', 'intent', 'medium', 'times', 'cue'} <= row.keys()
        assert all(t['polarity'] in ('offered', 'rejected') for t in row['times'])
    granularities = {t['granularity'] for row in PHRASES for t in row['times']}
    assert granularities == {'day', 'part_of_day', 'time', 'range', 'week'}
    assert {t['zone_source'] for row in PHRASES for t in row['times']} == {'explicit', 'owner', 'utc_fallback'}


# spec: ANYM-892
@pytest.mark.parametrize('row', PHRASES, ids=[row['id'] for row in PHRASES])
def test_phrase_fixture_row_passes_exactly(row):
    expected = {k: row[k] for k in ('intent', 'medium', 'cue', 'times')}
    assert observed(row, run(row)) == expected


# spec: ANYM-892
@pytest.mark.parametrize('text,expected', [
    ('во вторник не могу', [('вторник', 'rejected')]),
    ('во вторник не могу, давай в среду', [('вторник', 'rejected'), ('среду', 'offered')]),
    ("Tuesday doesn't work, how about Wednesday?", [('Tuesday', 'rejected'), ('Wednesday', 'offered')]),
])
def test_decline_marks_each_time_polarity(text, expected):
    got = signals(text, MONDAY, 'in', LISBON)
    assert got['intent'] == 'decline'
    assert [(text[slice(*t['span'])], t['polarity']) for t in got['times']] == expected


# spec: ANYM-892
@pytest.mark.parametrize('text,assumption,start', [
    ('Давай в 3', 'pm', '2026-11-09T15:00:00+00:00'),
    ('Давай в понедельник', 'next_week', '2026-11-16T00:00:00+00:00'),
    ('Давай в среду в 15:00', 'default_duration', '2026-11-11T15:00:00+00:00'),
])
def test_assumptions_are_named(text, assumption, start):
    (time,) = signals(text, MONDAY, 'in', LISBON)['times']
    assert assumption in time['assumed'] and time['start'] == start


# spec: ANYM-892
def test_default_duration_is_an_hour_and_half_that_for_a_call():
    meeting = signals('Встретимся в среду в 15:00', MONDAY, 'in', LISBON)['times'][0]
    call = signals('Созвонимся в среду в 15:00', MONDAY, 'in', LISBON)['times'][0]
    assert meeting['end'] == '2026-11-11T16:00:00+00:00' and call['end'] == '2026-11-11T15:30:00+00:00'
    assert signals('Давай в среду с 15 до 16', MONDAY, 'in', LISBON)['times'][0]['assumed'] == []


# spec: ANYM-892
def test_daylight_saving_change_resolves_to_the_new_offset():
    saturday = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
    (sunday,) = signals('Давай завтра в 10', saturday, 'in', LISBON)['times']
    (same,) = signals('Давай сегодня в 10', saturday, 'in', LISBON)['times']
    assert sunday['start'] == '2026-10-25T10:00:00+00:00' and same['start'] == '2026-10-24T10:00:00+01:00'


# spec: ANYM-892
def test_base_day_is_the_send_day_in_the_resolved_zone():
    late_evening_in_new_york = datetime(2026, 11, 10, 3, tzinfo=timezone.utc)
    ny = owner_zone({'ANYMSG_OWNER_TIMEZONE': 'America/New_York'})
    assert signals('давай завтра в 10', late_evening_in_new_york, 'in', ny)['times'][0]['start'] == '2026-11-10T10:00:00-05:00'
    assert signals('давай завтра в 10', late_evening_in_new_york, 'in', LISBON)['times'][0]['start'] == '2026-11-11T10:00:00+00:00'


# spec: ANYM-892
@pytest.mark.parametrize('text,zone', [('Давай в 10 МСК', 'Europe/Moscow'), ('Давай в 10 по московскому', 'Europe/Moscow'),
    ('call at 10 MSK', 'Europe/Moscow'), ('call at 10 UTC-5', 'Etc/GMT+5'), ('call at 10 GMT+1', 'Etc/GMT-1'),
    ('call at 10 UTC−2', 'Etc/GMT+2'), ('call at 10 UTC+14', 'Etc/GMT-14'), ('call at 10 WET', 'UTC'), ('call at 10 BST', 'Etc/GMT-1'),
    ('call at 10 CET', 'Etc/GMT-1'), ('call at 10 CEST', 'Etc/GMT-2'), ('call at 10 EEST', 'Etc/GMT-3'), ('call at 10 EST', 'Etc/GMT+5'),
    ('call at 10 PDT', 'Etc/GMT+7'), ('call at 10 PT', 'America/Los_Angeles'), ('call at 10 ET', 'America/New_York'), ('давай в 10 по Лиссабону', 'Europe/Lisbon'), ('call at 10 Lisbon time', 'Europe/Lisbon')])
def test_explicit_zone_markers(text, zone):
    (time,) = signals(text, MONDAY, 'in', LISBON)['times']
    assert (time['zone'], time['zone_source']) == (zone, 'explicit')


# spec: ANYM-892
@pytest.mark.parametrize('text,count', [('Call tomorrow morning London time or tomorrow at 12 MSK', 2), ('Давай завтра утром или в 10', 2),
    ('Давай утром или в 10', 2), ('tomorrow morning at 10', 1), ('Давай завтра утром в 10', 1), ('Утром в 10?', 1)])
def test_part_of_day_drops_only_when_its_own_clock_is_inside_it(text, count):
    assert len(signals(text, MONDAY, 'in', LISBON)['times']) == count


# spec: ANYM-892
def test_meeting_host_comes_from_the_parsed_hostname():
    text = 'Давай в 16:00 https://us02web.zoom.us/j/1 и https://meet.google.com/abc'
    assert signals(text, MONDAY, 'in', LISBON)['links'] == ['meet.google.com', 'zoom.us']


# spec: ANYM-892
def test_summer_abbreviation_keeps_its_own_offset_in_winter():
    (time,) = signals('Thursday 3pm CEST works for you?', MONDAY, 'in', LISBON)['times']
    assert time['start'] == '2026-11-12T15:00:00+02:00'


# spec: ANYM-892
def test_each_time_takes_its_own_zone_marker_and_base_day():
    got = signals('Call tomorrow at 10 ET or Friday at 10 PT', MONDAY, 'in', LISBON)
    assert [(t['start'], t['zone']) for t in got['times']] == [('2026-11-10T10:00:00-05:00', 'America/New_York'),
        ('2026-11-13T10:00:00-08:00', 'America/Los_Angeles')]


# spec: ANYM-892
@pytest.mark.parametrize('text', ['call at 10 Lİsbon time', 'call at 10 lısbon time', 'how about ſep 12', 'давай 12 ſентября',
    'call at 10 по Лİссабону'])
def test_case_folding_lookalikes_never_raise(text):
    got = signals(text, MONDAY, 'in', LISBON)
    assert all(t['zone'] in ZONES for t in got['times'])


# spec: ANYM-892
@pytest.mark.parametrize('text', ['Давай в 24:30', 'Call at 24pm', 'Call at 00pm', 'Call at 13pm', 'давай в 15 вечера', 'с 25 до 26',
    'Call at 10:75'])
def test_impossible_clock_values_yield_no_time(text):
    assert signals(text, MONDAY, 'in', LISBON)['times'] == []


# spec: ANYM-892
@pytest.mark.parametrize('sent_at,expected', [(datetime(2027, 11, 15, 10, tzinfo=timezone.utc), ['2028-02-29T00:00:00+00:00']),
    (datetime(2024, 11, 15, 10, tzinfo=timezone.utc), []), (datetime(2028, 2, 28, 10, tzinfo=timezone.utc), ['2028-02-29T00:00:00+00:00'])])
def test_leap_day_rolls_into_the_right_year(sent_at, expected):
    assert [t['start'] for t in signals('давай 29.02', sent_at, 'in', LISBON)['times']] == expected


# spec: ANYM-892
@pytest.mark.parametrize('text,sent_at', [('давай 31.12.9999', MONDAY), ('давай 12.11.2031', MONDAY),
    ('давай завтра в 23:30', datetime(9999, 12, 31, 12, tzinfo=timezone.utc)), ('на следующей неделе', datetime(9999, 12, 28, tzinfo=timezone.utc)),
    ('давай сегодня в 10', datetime(1, 1, 1, tzinfo=timezone.utc))])
def test_dates_far_from_the_send_day_or_at_the_calendar_edge_yield_no_time(text, sent_at):
    for zone in (LISBON, owner_zone({'ANYMSG_OWNER_TIMEZONE': 'Pacific/Pago_Pago'}), owner_zone({'ANYMSG_OWNER_TIMEZONE': 'Pacific/Kiritimati'})):
        got = signals(text, sent_at, 'in', zone)
        assert all(datetime.fromisoformat(t['start']).year in (sent_at.year, sent_at.year + 1) for t in got['times'])
    assert signals('давай 31.12.9999', MONDAY, 'in', LISBON)['times'] == []


# spec: ANYM-892
@pytest.mark.parametrize('text', ['Call https://zoom.us/j/tomorrow/SECRET/15:00', 'call https://zoom.us/j/secret?cancel=true',
    'Давай завтра https://zoom.us/j/15:00 в обед', 'https://meet.google.com/x?at=10:30 tomorrow'])
def test_link_paths_and_queries_are_never_parsed_or_spanned(text):
    got = signals(text, MONDAY, 'in', LISBON)
    link = re.search(r'https?://\S+', text).span()
    assert got['intent'] != 'cancel'
    assert all(e <= link[0] or s >= link[1] for s, e in spans(got))
    assert 'SECRET' not in json.dumps(got) and 'secret' not in json.dumps(got)


# spec: ANYM-892
@pytest.mark.parametrize('text', ['https://zoom.us.evil.test/j/1', 'https://zoom.us@evil.test/j/1', 'https://notzoom.us/j/1',
    'https://zoom.us:443@evil.test/j/1', 'https://evil.test/?next=https://zoom.us/j/1', 'https://evil.test/zoom.us/j/1'])
def test_lookalike_meeting_hosts_are_not_kept(text):
    assert signals(text, MONDAY, 'in', LISBON)['links'] == []


# spec: ANYM-892
@pytest.mark.parametrize('text,intent', [('Не получится, перенесём?', 'reschedule'), ('Can we reschedule?', 'reschedule'),
    ('Давай перенесём файлы на диск', 'none'), ('Мне нужно файлы перенести.', 'none'), ('Давай файлы перенесём?', 'none'),
    ('Может, перенесём?', 'reschedule'), ('We need to postpone', 'reschedule')])
def test_rescheduling_word_stands_alone_only_when_unambiguous(text, intent):
    assert signals(text, MONDAY, 'in', LISBON)['intent'] == intent


# spec: ANYM-892
@pytest.mark.parametrize('text,expected', [('Отменяем встречу в пятницу', ['rejected']),
    ('Отменим созвон в пятницу, давай в понедельник', ['rejected', 'offered']), ("Let's cancel Friday, let's do Monday", ['rejected', 'offered']),
    ("Cancel tomorrow at 10, let's do at 11", ['rejected', 'offered']), ('Отменим завтра в 10, давай в 11', ['rejected', 'offered'])])
def test_cancelled_times_are_rejected(text, expected):
    got = signals(text, MONDAY, 'in', LISBON)
    assert got['intent'] == 'cancel' and [t['polarity'] for t in got['times']] == expected


# spec: ANYM-892
@pytest.mark.parametrize('text', ['call at 10 GMT-14', 'call at 10 UTC+15', 'call at 10 UTC-13'])
def test_impossible_utc_offset_is_not_a_zone(text):
    (time,) = signals(text, MONDAY, 'in', LISBON)['times']
    assert (time['zone'], time['zone_source']) == ('Europe/Lisbon', 'owner')


# spec: ANYM-892
@pytest.mark.parametrize('text', ['wet weather, call at 10', 'call at 10 et cetera', 'go west, call at 10'])
def test_zone_abbreviations_that_are_words_need_capitals(text):
    (time,) = signals(text, MONDAY, 'in', LISBON)['times']
    assert (time['zone'], time['zone_source']) == ('Europe/Lisbon', 'owner')


# spec: ANYM-892
def test_city_table_is_fixed_and_small():
    assert len(scheduling_rules.CITIES) <= 40
    assert all(zone in ZONES for zone in scheduling_rules.CITIES.values())


# spec: ANYM-892
def test_negative_fixture_has_the_required_rows():
    assert len(NEGATIVES) >= 50
    assert len({row['id'] for row in NEGATIVES}) == len(NEGATIVES)


# spec: ANYM-892
@pytest.mark.parametrize('row', NEGATIVES, ids=[row['id'] for row in NEGATIVES])
def test_negative_fixture_row_yields_no_time_and_no_intent(row):
    got = run(row)
    assert (got['intent'], got['times']) == (row['intent'], row['times']) == ('none', [])


# spec: ANYM-892
def test_meeting_link_keeps_only_its_host():
    got = signals('Давай в 16:00, ссылка https://us02web.zoom.us/j/123?pwd=secret', MONDAY, 'in', LISBON)
    assert got['medium'] == 'video_call' and got['links'] == ['zoom.us']
    assert 'secret' not in json.dumps(got)


# spec: ANYM-892
def test_message_that_is_only_a_link_has_no_intent():
    got = signals('https://meet.google.com/abc-defg-hij', MONDAY, 'in', LISBON)
    assert (got['intent'], got['medium'], got['links'], got['times']) == ('none', 'video_call', ['meet.google.com'], [])


# spec: ANYM-892
def test_confidence_levels():
    assert signals('Давай созвонимся в среду в 15:00', MONDAY, 'in', LISBON)['confidence'] == 'high'
    assert signals('Давай в среду в 15:00', MONDAY, 'in', LISBON)['confidence'] == 'medium'
    assert signals('Созвонимся в среду в 15:00?', MONDAY, 'in', LISBON)['confidence'] == 'low'  # no cue word
    assert signals('ок', MONDAY, 'in', LISBON)['confidence'] == 'low'


# spec: ANYM-892
def test_recurrence_resolves_to_the_next_occurrence_only():
    got = signals('Каждый вторник в 10 созвон', MONDAY, 'in', LISBON)
    assert [t['start'] for t in got['times']] == ['2026-11-10T10:00:00+00:00'] and 'recurrence' not in got


# spec: ANYM-892
def test_shared_word_tables_come_from_time_references():
    assert scheduling_rules.WEEKDAYS is time_references.WEEKDAYS
    assert scheduling_rules.PAST_BEFORE is time_references.PAST_BEFORE


FRAGMENTS = ['давай', 'во вторник', 'не могу', 'в 3', 'в 15:00', 'завтра', 'после обеда', 'на следующей неделе', '12.11',
    '12 ноября', 'по Москве', 'МСК', 'UTC+3', 'GMT-14', 'UTC+99', 'how about', 'Thursday', '3pm', 'between 2 and 4', '2-4pm',
    '10:30-11:30', "doesn't work", 'next week', 'next Monday', 'the 31st', 'Feb 30', '29.02', 'Lisbon time', 'по Лиссабону',
    'созвон', 'zoom', 'https://zoom.us/j/1', 'перенесём', 'отмена', 'last Friday', '> ', '\n', ', ', '. ', '?', 'с 23 до 1']
TEXT = st.lists(st.one_of(st.sampled_from(FRAGMENTS), st.text(max_size=12)), max_size=24).map(' '.join)
SENT = st.one_of(st.datetimes(timezones=st.just(timezone.utc)),
    st.datetimes(min_value=datetime(2020, 1, 1), max_value=datetime(2040, 12, 31), timezones=st.just(timezone.utc)))
OWNER = st.sampled_from([time_references.UTC_FALLBACK, LISBON, owner_zone({'ANYMSG_OWNER_TIMEZONE': 'Pacific/Kiritimati'}),
    owner_zone({'ANYMSG_OWNER_TIMEZONE': 'Pacific/Pago_Pago'}), owner_zone({'ANYMSG_OWNER_TIMEZONE': 'Europe/Moscow'})])
DIRECTION = st.sampled_from(['in', 'out'])


def spans(got):
    return [t['span'] for t in got['times']] + ([got['cue_span']] if got['cue_span'] else [])


# spec: ANYM-892
@settings(max_examples=300, deadline=None)
@given(text=st.one_of(TEXT, st.text()), sent_at=SENT, direction=DIRECTION, zone=OWNER)
def test_property_never_raises_and_is_deterministic(text, sent_at, direction, zone):
    assert signals(text, sent_at, direction, zone) == signals(text, sent_at, direction, zone)


# spec: ANYM-892
@settings(max_examples=300, deadline=None)
@given(text=st.one_of(TEXT, st.text()), sent_at=SENT, zone=OWNER)
def test_property_spans_lie_inside_the_input_and_zones_are_iana(text, sent_at, zone):
    got = signals(text, sent_at, 'in', zone)
    assert all(0 <= start < end <= min(len(text), MAX_CHARS) for start, end in spans(got))
    links = [m.span() for m in re.finditer(r'https?://\S+', text[:MAX_CHARS], re.I)]
    assert all(e <= ls or s >= le for s, e in spans(got) for ls, le in links)
    assert all(t['zone'] in ZONES for t in got['times'])
    assert all(datetime.fromisoformat(t['start']) < datetime.fromisoformat(t['end']) for t in got['times'])


# spec: ANYM-892
@settings(max_examples=300, deadline=None)
@given(text=TEXT, sent_at=SENT, zone=OWNER)
def test_property_forward_moves_start_on_or_after_the_send_day(text, sent_at, zone):
    got = signals(text, sent_at, 'in', zone)
    if got['intent'] in ('propose', 'ask_availability', 'reschedule'):
        for t in got['times']:
            send_day = sent_at.astimezone(ZoneInfo(t['zone'])).date()
            assert datetime.fromisoformat(t['start']).date() >= send_day


# spec: ANYM-892
@settings(max_examples=30, deadline=None)
@given(head=st.one_of(TEXT, st.text(min_size=1)), sent_at=SENT, zone=OWNER)
def test_property_input_is_truncated_before_matching(head, sent_at, zone):
    big = (head * (65536 // len(head) + 1))[:65536] if head else 'x' * 65536
    assert len(big) == 65536
    assert signals(big, sent_at, 'in', zone) == signals(big[:MAX_CHARS], sent_at, 'in', zone)


# spec: ANYM-892
def test_long_input_stays_fast():
    start = perf_counter()
    signals('давай в 10 во вторник, не могу в 3. ' * 2000, MONDAY, 'in', LISBON)
    assert perf_counter() - start < 2


# spec: ANYM-892
@pytest.mark.parametrize('unit', ['давай в 10 во вторник, не могу в 3. ', 'today 1pm. ', 'today 1pm ', 'в 10 ', 'завтра ',
    'с 1 до 2 ', '12.11 ', 'https://zoom.us/j/1 ', 'по Москве в 10 '])
def test_dense_input_stays_fast(unit):
    text = unit * (MAX_CHARS // len(unit) + 1)
    start = perf_counter()
    signals(text, MONDAY, 'in', LISBON)
    assert perf_counter() - start < 1.0
