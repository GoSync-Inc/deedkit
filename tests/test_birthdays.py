"""Telegram birthdays of active contacts (ANYM-919): the active rule, the collector's birthday pass, the table and
list_birthdays. Synthetic data on the isolated test database; no provider is ever reached."""
import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace as NS

from psycopg.types.json import Jsonb
import pytest

from messaging_service import birthdays
from messaging_service.archive import Archive
from messaging_service.blobs import BlobWriter
from messaging_service.collector_state import CaptureSpool, CollectorState
from messaging_service.telegram_birthdays import BirthdayProvider
from messaging_service.telegram_collector import TelegramCollector
from messaging_service.telegram_governor import ProviderRateLimited

ACCOUNT = 'tg-bd'
SELF_ID = '999000999'


def contact(c, user, *, outgoing=3, incoming=1, incoming_days=1, outgoing_days=1, bot=False, title=None):
    """A private chat with `user` holding `outgoing` and `incoming` messages that many days ago."""
    now = datetime.now(timezone.utc)
    chat = 'bd-chat-' + user
    c.execute("INSERT INTO chats(id,account,provider_chat,title,chat_type) VALUES(%s,%s,%s,%s,'private')",
        (chat, ACCOUNT, user, title or 'Синтетик ' + user))
    rows = [('outgoing', outgoing_days)] * outgoing + [('incoming', incoming_days)] * incoming
    for n, (direction, days) in enumerate(rows):
        mid = 'bd-%s-%d' % (user, n)
        c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,direction)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s)''', (mid, ACCOUNT, chat, str(n + 1),
            SELF_ID if direction == 'outgoing' else user, 'synthetic', now - timedelta(days=days, minutes=n), direction))
        if bot and direction == 'incoming':
            c.execute('INSERT INTO message_metadata(message,revision,metadata) VALUES(%s,1,%s)',
                (mid, Jsonb({'sender_is_bot': True})))
    return chat


@pytest.fixture
def bd(seeded, tmp_path, monkeypatch):
    monkeypatch.delenv('ANYMSG_TELEGRAM_BIRTHDAYS', raising=False)
    store, _, _ = seeded
    archive = Archive(store)
    archive.register(ACCOUNT, 'telegram', 'Synthetic Telegram birthdays', discover=True)
    worker = TelegramCollector(archive, ACCOUNT, BirthdayFake(), CaptureSpool(tmp_path / 'bd', reserve_bytes=0),
        writer=BlobWriter(store.files, reserve_bytes=0), owner='fixture-owner')
    epoch = archive.acquire(ACCOUNT, 'fixture-owner')
    worker.state = CollectorState(archive, ACCOUNT, ('fixture-owner', epoch))
    worker.policy = worker.state.policy()
    return worker, store


class BirthdayFake:
    """A provider with the two birthday reads; records each full-user read."""

    def __init__(self, known=None, hidden=(), limited_at=None, wait=30, contacts=None):
        self.known, self.hidden = known or {}, set(hidden)
        self.limited_at, self.wait, self.contacts = limited_at, wait, contacts or {}
        self.reads, self.contact_reads = [], 0

    def self_provider_id(self):
        return SELF_ID

    async def full_user_birthday(self, user):
        self.reads.append(user)
        if self.limited_at is not None and len(self.reads) == self.limited_at:
            self.limited_at = None
            raise ProviderRateLimited(self.wait)
        return None if user in self.hidden else self.known.get(user, (int(user) % 28 + 1, 5, None))

    async def contact_birthdays(self):
        self.contact_reads += 1
        return dict(self.contacts)


def rows(store):
    with store.pool.connection() as c:
        return {r['provider_user']: r for r in c.execute(
            'SELECT * FROM contact_birthdays WHERE account=%s', (ACCOUNT,)).fetchall()}


# spec: ANYM-919
def test_active_is_three_outgoing_and_one_incoming_in_ninety_days_without_bots_or_the_self_chat(bd):
    _, store = bd
    with store.pool.connection() as c:
        contact(c, '101', incoming_days=89)                 # 3 out + 1 in at day 89: active
        contact(c, '102', outgoing=2, incoming=5)           # 2 out + 5 in: not
        contact(c, '103', incoming=0)                       # 3 out + 0 in: not
        contact(c, '104', incoming_days=91)                 # 3 out + 1 in at day 91: not
        contact(c, '105', bot=True)                         # a bot chat: not
        contact(c, '106', outgoing_days=91)                 # the outgoing messages are older than 90 days: not
        contact(c, SELF_ID)                                 # the self chat: not
        contact(c, '777000')                                # Telegram's service chat: not
        found = birthdays.active_contacts(c, ACCOUNT, self_id=SELF_ID)
    assert [(r['provider_user'], r['chat'], r['name']) for r in found] == [('101', 'bd-chat-101', 'Синтетик 101')]


# spec: ANYM-919
def test_deleted_messages_do_not_make_a_contact_active(bd):
    _, store = bd
    with store.pool.connection() as c:
        contact(c, '107')
        c.execute("UPDATE messages SET deleted=true WHERE id='bd-107-3'")  # the only incoming message
        assert birthdays.active_contacts(c, ACCOUNT, self_id=SELF_ID) == []


# spec: ANYM-919
def test_a_hundred_active_users_give_at_most_thirty_full_user_reads_an_hour(bd):
    worker, store = bd
    with store.pool.connection() as c:
        for n in range(100):
            contact(c, str(1000 + n))
    for _ in range(3):
        assert asyncio.run(worker.birthdays_pass()) == 0
    assert len(worker.provider.reads) == 30 and len(set(worker.provider.reads)) == 30
    assert len(rows(store)) == 30
    # A new collector after a restart takes its budget from the stored check times, not a fresh one.
    fresh = TelegramCollector(worker.archive, ACCOUNT, worker.provider, worker.spool, writer=worker.writer,
        owner='fixture-owner')
    fresh.state, fresh.policy = worker.state, worker.policy
    asyncio.run(fresh.birthdays_pass())
    assert len(worker.provider.reads) == 30
    # An hour later the next 30 unread users are read; the read ones are not read again within 7 days.
    with store.pool.connection() as c:
        c.execute("UPDATE contact_birthdays SET checked_at=checked_at-interval '61 minutes'")
    asyncio.run(worker.birthdays_pass())
    assert len(worker.provider.reads) == 60 and len(set(worker.provider.reads)) == 60
    # Once every user is read, a week-old birthday is read again, within the same hourly budget.
    first = list(worker.provider.reads)
    with store.pool.connection() as c:
        for n in range(100):
            if str(1000 + n) not in first:
                birthdays.store(c, ACCOUNT, str(1000 + n), None, now=datetime.now(timezone.utc) - timedelta(hours=2))
        c.execute("UPDATE contact_birthdays SET checked_at=checked_at-interval '7 days' WHERE provider_user=ANY(%s)",
            (first,))
    asyncio.run(worker.birthdays_pass())
    assert len(worker.provider.reads) == 90 and set(worker.provider.reads[60:]) <= set(first)


# spec: ANYM-919
def test_a_flood_wait_on_read_five_stops_the_pass_until_the_wait_ends(bd):
    worker, store = bd
    worker.provider = BirthdayFake(limited_at=5, wait=1)
    with store.pool.connection() as c:
        for n in range(40):
            contact(c, str(2000 + n))
    assert asyncio.run(worker.birthdays_pass()) == 1
    assert len(worker.provider.reads) == 5 and len(rows(store)) == 4

    async def loop():
        worker.birthdays_interval = 0.01
        worker.provider = BirthdayFake(limited_at=1, wait=1)
        task = asyncio.create_task(worker.birthdays_loop())
        await asyncio.sleep(.5)
        during = len(worker.provider.reads)
        await asyncio.sleep(1.2)
        worker.stopping.set()
        await asyncio.wait_for(task, 5)
        return during, len(worker.provider.reads)
    during, after = asyncio.run(loop())
    # Reads 6..30 wait for the FloodWait; after it the pass resumes within the hour's remaining budget (30-4).
    assert during == 1 and after == 1 + 26


# spec: ANYM-919
def test_a_user_who_hides_the_birthday_is_stored_hidden_and_a_shared_one_known(bd):
    worker, store = bd
    worker.provider = BirthdayFake(known={'3001': (29, 2, 1990), '3002': (1, 1, None)}, hidden={'3003'})
    with store.pool.connection() as c:
        for user in ('3001', '3002', '3003'):
            contact(c, user)
    assert asyncio.run(worker.birthdays_pass()) == 0
    found = rows(store)
    assert {u: (r['state'], r['day'], r['month'], r['year']) for u, r in found.items()} == {
        '3001': ('known', 29, 2, 1990), '3002': ('known', 1, 1, None), '3003': ('hidden', None, None, None)}


# spec: ANYM-919
def test_get_birthdays_is_read_every_six_hours_and_stores_only_active_users(bd):
    worker, store = bd
    worker.provider = BirthdayFake(contacts={'3101': (11, 11, None), '3999': (12, 11, 1980)})
    with store.pool.connection() as c:
        contact(c, '3101')
    asyncio.run(worker.birthdays_pass())
    asyncio.run(worker.birthdays_pass())
    assert worker.provider.contact_reads == 1
    # The contact it answered for is fresh: no full-user read; the inactive 3999 is never stored.
    assert worker.provider.reads == [] and set(rows(store)) == {'3101'}


# spec: ANYM-919
def test_a_user_who_stops_being_active_is_deleted_at_the_next_pass(bd):
    worker, store = bd
    with store.pool.connection() as c:
        contact(c, '4001')
        contact(c, '4002')
        contact(c, '4003')
    asyncio.run(worker.birthdays_pass())
    assert set(rows(store)) == {'4001', '4002', '4003'}
    with store.pool.connection() as c:
        c.execute("UPDATE messages SET sent_at=sent_at-interval '100 days' WHERE chat='bd-chat-4001' AND direction='incoming'")
    # The collection policy now excludes 4002: it is no longer an active contact of the archive either.
    worker.policy = {**worker.policy, 'exclude_chats': ['4002']}
    asyncio.run(worker.birthdays_pass())
    assert set(rows(store)) == {'4003'}


# spec: ANYM-919
def test_the_kill_switch_stops_every_read(bd, monkeypatch):
    worker, store = bd
    monkeypatch.setenv('ANYMSG_TELEGRAM_BIRTHDAYS', '0')
    with store.pool.connection() as c:
        contact(c, '4101')
    assert asyncio.run(worker.birthdays_pass()) == 0
    assert worker.provider.reads == [] and worker.provider.contact_reads == 0 and rows(store) == {}


# spec: ANYM-919
def test_the_telethon_reads_map_birthday_hidden_and_refusals():
    from telethon import errors, functions, types
    from telethon.tl.types import contacts, users

    class Client:
        def __init__(self, answer):
            self.answer, self.calls = answer, []

        async def get_input_entity(self, peer):
            return types.InputPeerUser(peer, 7)

        async def __call__(self, request):
            self.calls.append(type(request))
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

    class Provider(BirthdayProvider):
        def __init__(self, answer):
            self.client = Client(answer)

        async def rpc(self, operation, phase, *, tier=None):
            return await operation()

    def full(birthday):
        return users.UserFull(full_user=types.UserFull(id=5, settings=types.PeerSettings(),
            notify_settings=types.PeerNotifySettings(), common_chats_count=0, birthday=birthday), chats=[], users=[])
    assert asyncio.run(Provider(full(types.Birthday(29, 2, 1992))).full_user_birthday('5')) == (29, 2, 1992)
    assert asyncio.run(Provider(full(types.Birthday(3, 4))).full_user_birthday('5')) == (3, 4, None)
    assert asyncio.run(Provider(full(None)).full_user_birthday('5')) is None
    assert asyncio.run(Provider(full(types.Birthday(31, 4))).full_user_birthday('5')) is None
    provider = Provider(errors.UserIdInvalidError(request=None))
    assert asyncio.run(provider.full_user_birthday('5')) is None
    assert provider.client.calls == [functions.users.GetFullUserRequest]
    with pytest.raises(errors.FloodWaitError):
        asyncio.run(Provider(errors.FloodWaitError(request=None, capture=30)).full_user_birthday('5'))
    answer = contacts.ContactBirthdays(contacts=[types.ContactBirthday(5, types.Birthday(1, 2, 2000)),
        types.ContactBirthday(6, types.Birthday(30, 2))], users=[])
    provider = Provider(answer)
    assert asyncio.run(provider.contact_birthdays()) == {'5': (1, 2, 2000)}
    assert provider.client.calls == [functions.contacts.GetBirthdaysRequest]


def known(c, user, day, month, year=None):
    birthdays.store(c, ACCOUNT, user, (day, month, year))


# spec: ANYM-919
def test_list_birthdays_lists_29_february_on_28_february_of_a_common_year(bd):
    _, store = bd
    with store.pool.connection() as c:
        contact(c, '5001')
        known(c, '5001', 29, 2, 1990)
        result = birthdays.upcoming(c, ACCOUNT, date(2027, 2, 20), 14, self_id=SELF_ID)
        leap = birthdays.upcoming(c, ACCOUNT, date(2028, 2, 20), 14, self_id=SELF_ID)
    assert result['birthdays'] == [{'name': 'Синтетик 5001', 'chat_id': 'bd-chat-5001', 'day': 29, 'month': 2,
        'year': 1990, 'date': '2027-02-28', 'days_until': 8, 'age_turning': 37}]
    assert leap['birthdays'][0]['date'] == '2028-02-29' and leap['birthdays'][0]['days_until'] == 9


# spec: ANYM-919
def test_list_birthdays_sorts_across_the_year_boundary_and_counts_not_shared_without_names(bd):
    _, store = bd
    with store.pool.connection() as c:
        for user in ('5101', '5102', '5103', '5104', '5105', '5106'):
            contact(c, user)
        known(c, '5101', 2, 1)
        known(c, '5102', 31, 12, 2000)
        known(c, '5103', 1, 1)
        known(c, '5104', 20, 1)  # outside 14 days: neither listed nor counted
        birthdays.store(c, ACCOUNT, '5105', None)  # hidden
        # 5106 is not read yet
        result = birthdays.upcoming(c, ACCOUNT, date(2026, 12, 30), 14, self_id=SELF_ID)
    assert [(b['chat_id'], b['date'], b['days_until'], b.get('age_turning'), b['year']) for b in result['birthdays']] == [
        ('bd-chat-5102', '2026-12-31', 1, 26, 2000), ('bd-chat-5103', '2027-01-01', 2, None, None),
        ('bd-chat-5101', '2027-01-02', 3, None, None)]
    assert result['not_shared'] == 2 and result['truncated'] is False
    assert result['source'] == 'active_telegram_contacts' and result['calendar'] == 'not_written'
    assert 'Синтетик 5105' not in str(result) and 'Синтетик 5106' not in str(result)
    with pytest.raises(ValueError):
        birthdays.upcoming(None, ACCOUNT, date(2026, 12, 30), 32)


# spec: ANYM-919
def test_a_contact_whose_chat_the_key_cannot_read_is_neither_listed_nor_counted(bd):
    worker, store = bd
    with store.pool.connection() as c:
        for user in ('5201', '5202', '5203', '5204'):
            contact(c, user)
        known(c, '5201', 12, 11)
        known(c, '5202', 13, 11)
        birthdays.store(c, ACCOUNT, '5204', None)
    scoped = store.issue_key('bd-scoped', [ACCOUNT], chats=['bd-chat-5201', 'bd-chat-5203'],
        permissions=['contacts-read'], key_permissions=['contacts-read'])
    full = store.issue_key('bd-full', [ACCOUNT], permissions=['contacts-read'], key_permissions=['contacts-read'])
    assert scoped and full

    def listed(principal, policy=None):
        job = {'operation': 'list_birthdays', 'account': ACCOUNT, 'principal': principal,
            'parameters': {'days_ahead': 31}, '_policy': policy or worker.policy}
        return asyncio.run(worker.list_birthdays(job))['data']
    data = listed('bd-scoped')
    # 5202 (known) and 5204 (hidden) are outside the key's chats: no entry and no count.
    assert [b['chat_id'] for b in data['birthdays']] == ['bd-chat-5201'] and data['not_shared'] == 1
    whole = listed('bd-full')
    assert [b['chat_id'] for b in whole['birthdays']] == ['bd-chat-5201', 'bd-chat-5202'] and whole['not_shared'] == 2
    # A chat the collection policy excludes is gone for every key.
    excluded = listed('bd-full', {**worker.policy, 'exclude_chats': ['5202', '5204']})
    assert [b['chat_id'] for b in excluded['birthdays']] == ['bd-chat-5201'] and excluded['not_shared'] == 1
    assert data['time_zone'] == 'UTC_fallback' and data['today']
    from messaging_service.commands import CommandRejected
    with pytest.raises(CommandRejected):
        listed('bd-nobody')


# spec: ANYM-919
def test_list_birthdays_is_a_telegram_account_read_a_chat_scoped_key_may_call():
    from messaging_service.messenger_operations import OPERATION_TOOLS, operation_manifest, validate_operation
    telegram = {row['operation']: row for row in operation_manifest('telegram')}['list_birthdays']
    whatsapp = {row['operation']: row for row in operation_manifest('whatsapp')}['list_birthdays']
    assert telegram['supported'] and not whatsapp['supported']
    assert (telegram['permission'], telegram['target'], telegram['mutation'], telegram['full_account']) == (
        'contacts-read', 'account', False, False)
    assert 'active Telegram contacts only' in telegram['limitation'] and 'nothing to any calendar' in telegram['limitation']
    assert 'list_birthdays' in OPERATION_TOOLS['read_contacts']
    assert validate_operation('telegram', 'list_birthdays', {}) == {'days_ahead': 14}
    for bad in (-1, 32, 1.5, '3'):
        with pytest.raises(ValueError):
            validate_operation('telegram', 'list_birthdays', {'days_ahead': bad})


# spec: ANYM-919
def test_the_gateway_catalog_says_active_contacts_only_and_no_calendar():
    import json
    from pathlib import Path
    catalog = {t['name']: t for t in json.loads((Path(__file__).resolve().parents[1] /
        'services/gateway/src/agent_gateway/catalog.json').read_text())}
    contacts = catalog['messages_read_contacts']
    text = contacts['description'] + contacts['inputSchema']['properties']['parameters']['description']
    assert 'list_birthdays' in contacts['routes_actions'] and 'list_birthdays' in contacts['examples']
    assert "owner's active Telegram contacts" in text and 'nothing to any calendar' in text
    create = next(t for t in catalog.values() if 'contact.birthday' in json.dumps(t.get('inputSchema', {})))
    assert "active Telegram contacts only" in create['description'] and 'nothing to any calendar' in create['description']


# spec: ANYM-919
def test_the_hint_is_due_from_nine_owner_time_for_tomorrow():
    from zoneinfo import ZoneInfo
    zone = ZoneInfo('Asia/Makassar')
    assert birthdays.hint_date(datetime(2026, 11, 11, 0, 59, tzinfo=timezone.utc), zone) is None  # 08:59 local
    assert birthdays.hint_date(datetime(2026, 11, 11, 1, 0, tzinfo=timezone.utc), zone) == date(2026, 11, 12)
    assert birthdays.hint_date(datetime(2026, 11, 11, 15, 59, tzinfo=timezone.utc), zone) == date(2026, 11, 12)
    assert birthdays.hint_date(datetime(2026, 11, 11, 16, 0, tzinfo=timezone.utc), zone) is None  # 00:00 local


# spec: ANYM-919
def test_the_matcher_skips_the_birthday_hint_until_schema_87_and_never_raises(bd):
    _, store = bd
    with store.pool.connection() as c:
        head = c.execute('SELECT value FROM feed_counter WHERE id=1').fetchone()['value']
    noon = datetime(2026, 11, 11, 4, 0, tzinfo=timezone.utc)
    env = {'ANYMSG_OWNER_TIMEZONE': 'Asia/Makassar'}
    # Schema 86 has no journal function: the call is skipped, nothing is journaled, no error is logged.
    assert birthdays.journal_birthday_hints(store, now=noon, environ=env) is None
    assert birthdays.journal_birthday_hints(store, now=noon - timedelta(hours=4), environ=env) == 0  # 08:00 local
    broken = NS(pool=NS(connection=lambda: (_ for _ in ()).throw(OSError('down'))))
    assert birthdays.journal_birthday_hints(broken, now=noon, environ=env) is None
    with store.pool.connection() as c:
        assert c.execute('SELECT value FROM feed_counter WHERE id=1').fetchone()['value'] == head
        assert c.execute("SELECT count(*) AS n FROM events WHERE kind='contact.birthday'").fetchone()['n'] == 0
