from datetime import datetime,timezone

import pytest

from messaging_service.time_references import owner_zone,passed_reference


def at(day,hour=10,minute=0):return datetime(2026,10,day,hour,minute,tzinfo=timezone.utc)

UTC=owner_zone({})
MONDAY=at(12)  # 2026-10-12 is a Monday; that week's Friday is the 16th


# spec: ANYM-282
def test_queue_flags_passed_weekday_reference():
    flag=passed_reference('сможешь в пятницу?',MONDAY,at(20),UTC)
    assert flag=={'text':'в пятницу','resolved_date':'2026-10-16','status':'passed','time_zone':'UTC_fallback',
        'suggest':'acknowledge_delay_offer_new_date'}


# spec: ANYM-282
def test_queue_future_reference_not_flagged():
    assert passed_reference('сможешь в пятницу?',MONDAY,at(14),UTC) is None
    assert passed_reference('сможешь в пятницу?',MONDAY,at(16,23),UTC) is None


# spec: ANYM-282
@pytest.mark.parametrize('text',['как-нибудь потом','в выходные?','next week maybe?','minggu depan ya',
    'версия 5.10 вышла','Friday was fun','в прошлую пятницу было здорово','last Friday?','Jumat lalu kenapa?',
    'вчера звонил','yesterday?','kemarin ya','в понедельник?'])
def test_queue_ambiguous_reference_not_flagged(text):
    # The last one is a weekday equal to the send day: today or next week is a guess.
    assert passed_reference(text,MONDAY,at(30),UTC) is None


# spec: ANYM-282
def test_queue_time_reference_utc_fallback():
    thursday_late=datetime(2026,10,15,23,30,tzinfo=timezone.utc)
    assert passed_reference('завтра?',thursday_late,at(20),UTC)['resolved_date']=='2026-10-16'
    bali=owner_zone({'ANYMSG_OWNER_TIMEZONE':'Asia/Makassar'})
    flag=passed_reference('завтра?',thursday_late,at(20),bali)
    assert flag['resolved_date']=='2026-10-17' and flag['time_zone']=='Asia/Makassar'
    assert owner_zone({'ANYMSG_OWNER_TIMEZONE':'Mars/Olympus'})==UTC
    assert owner_zone({'ANYMSG_OWNER_TIMEZONE':'../etc/passwd'})==UTC


# spec: ANYM-282
@pytest.mark.parametrize('text,expected',[
    ('зайдёшь сегодня?','2026-10-12'),('can you visit today?','2026-10-12'),('bisa datang hari ini?','2026-10-12'),
    ('tomorrow ok?','2026-10-13'),('besok bisa?','2026-10-13'),('послезавтра удобно?','2026-10-14'),
    ('day after tomorrow?','2026-10-14'),('lusa ya?','2026-10-14'),('can we meet on Friday?','2026-10-16'),
    ('send it by Thursday','2026-10-15'),('ketemu hari Jumat?','2026-10-16'),('Kamis ini bisa?','2026-10-15'),
    ('сделаешь до среды?','2026-10-14'),('во вторник?','2026-10-13'),('до 17.10 успеешь?','2026-10-17'),
    ('by 15/10 please','2026-10-15'),('встреча 14.10.2026','2026-10-14'),('tanggal 13.10 ya','2026-10-13')])
def test_queue_flags_ru_en_id_phrases(text,expected):
    flag=passed_reference(text,MONDAY,at(25),UTC)
    assert flag and flag['resolved_date']==expected and flag['status']=='passed'


# spec: ANYM-282
def test_queue_today_reference_passes_next_day_and_latest_date_decides():
    sixteenth=at(16,9)
    assert passed_reference('зайдёшь сегодня?',sixteenth,at(16,22),UTC) is None
    assert passed_reference('зайдёшь сегодня?',sixteenth,at(17,1),UTC)['resolved_date']=='2026-10-16'
    # A later reference that has not passed keeps the request current.
    assert passed_reference('сегодня или до 30.10?',sixteenth,at(20),UTC) is None
    assert passed_reference(None,sixteenth,at(20),UTC) is None
    assert len(passed_reference('сегодня '+'x'*200,sixteenth,at(20),UTC)['text'])<=40


# spec: ANYM-282
@pytest.mark.parametrize('text',['on Frİday?','on Frıday?','on ſunday?','hari Jumat lalu?','on Friday last week?',
    'в пятницу на прошлой неделе?','в пятницу прошлой недели?','на прошлой неделе в пятницу?'])
def test_queue_lookalike_or_past_weekday_yields_nothing(text):
    assert passed_reference(text,MONDAY,at(20),UTC) is None
    assert passed_reference('сегодня? '+text,MONDAY,at(20),UTC)['resolved_date']=='2026-10-12'


# spec: ANYM-282
@pytest.mark.parametrize('text,expected',[('к пятнице?','2026-10-16'),('к среде?','2026-10-14'),('к субботе?','2026-10-17'),
    ('к воскресенью?','2026-10-18'),('ко вторнику?','2026-10-13'),('к четвергу?','2026-10-15')])
def test_queue_flags_russian_by_weekday_deadlines(text,expected):
    assert passed_reference(text,MONDAY,at(25),UTC)['resolved_date']==expected
    # A still-future deadline next to "today" keeps the request current.
    assert passed_reference('сегодня или '+text,MONDAY,at(13),UTC) is None


# spec: ANYM-282
def test_queue_numeric_date_rolls_into_a_leap_year_and_rejects_invalid_dates():
    sent=datetime(2027,11,15,10,tzinfo=timezone.utc)
    assert passed_reference('до 29.02?',sent,datetime(2028,3,1,tzinfo=timezone.utc),UTC)['resolved_date']=='2028-02-29'
    assert passed_reference('до 31.02?',sent,datetime(2028,3,1,tzinfo=timezone.utc),UTC) is None
    assert passed_reference('до 29.02?',datetime(2026,11,15,tzinfo=timezone.utc),datetime(2027,3,1,tzinfo=timezone.utc),UTC) is None


# spec: ANYM-282
def test_queue_parser_error_drops_the_flag_not_the_read():
    assert passed_reference('сегодня?','not a datetime',at(20),UTC) is None


# spec: ANYM-282
def test_queue_past_qualifier_stays_within_the_weekday_clause():
    for text in ('on Friday of last week?','hari Jumat minggu lalu?'):
        assert passed_reference(text,MONDAY,at(20),UTC) is None
        assert passed_reference('сегодня? '+text,MONDAY,at(20),UTC)['resolved_date']=='2026-10-12'
    # A past phrase in the next sentence does not cancel a future Friday.
    assert passed_reference('today or on Friday? Last week was busy.',MONDAY,at(14),UTC) is None
    assert passed_reference('Last week was busy. Today or on Friday?',MONDAY,at(14),UTC) is None


# spec: ANYM-282
@pytest.mark.parametrize('text',['on Friday (last week)?','on Friday\nlast week?','в пятницу (на прошлой неделе)?',
    'в пятницу\nна прошлой неделе?','hari Jumat (lalu)?','(last week) on Friday?','на прошлой неделе — в пятницу?'])
def test_queue_past_qualifier_crosses_brackets_and_line_breaks(text):
    assert passed_reference(text,MONDAY,at(20),UTC) is None
