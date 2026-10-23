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
