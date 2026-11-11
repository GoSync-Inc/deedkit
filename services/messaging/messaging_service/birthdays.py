"""Telegram birthdays of the contacts the owner actively talks to (ANYM-919; owner 2026-11-09: only active
contacts, reminders, nothing written to any calendar).

Active (PEOPLE.md "Birthdays of active contacts"): a Telegram user is active for an account when, in the private
chat with that user, the archive holds at least 3 outgoing and at least 1 incoming message sent in the last 90
days, not deleted; the self chat, the Telegram service chat and a chat whose incoming message in that window
Telegram marked as sent by a bot are excluded. The collector's birthday pass (telegram_birthdays.py) keeps
`contact_birthdays` to exactly those users: it reads a missing or week-old birthday, stores `known` or `hidden`,
and deletes the row of a user who is no longer active. `list_birthdays` reads the upcoming known ones. The matcher
journals the `contact.birthday` hint once schema 89's definer function exists; until then it skips.
A birthday is data a contact chose; logs carry counts only.
"""
from datetime import date, datetime, timedelta, timezone
import json
import logging

from .chat_queries import peer_name_sql, raw_title_sql, self_chat_sql

ACTIVE_DAYS = 90
MIN_OUTGOING = 3
MIN_INCOMING = 1
STALE_AFTER = timedelta(days=7)
READS_PER_HOUR = 30
DAYS_AHEAD = 14
MAX_DAYS_AHEAD = 31
MAX_LISTED = 100
HINT_HOUR = 9
SERVICE_CHAT = '777000'
log = logging.getLogger(__name__)

ACTIVE_SQL = f'''SELECT c.id AS chat,c.provider_chat AS provider_user,
    CASE WHEN {raw_title_sql('c')} THEN coalesce({peer_name_sql('c')},c.title) ELSE c.title END AS name
  FROM chats c JOIN LATERAL (SELECT count(*) FILTER (WHERE m.direction='outgoing') AS outgoing,
      count(*) FILTER (WHERE m.direction='incoming') AS incoming,
      coalesce(bool_or(m.direction='incoming' AND md.metadata->'sender_is_bot'='true'::jsonb),false) AS bot
    FROM messages m LEFT JOIN message_metadata md ON md.message=m.id AND md.revision=m.revision
    WHERE m.account=c.account AND m.chat=c.id AND m.sent_at>%(since)s AND NOT m.deleted) r ON true
  WHERE c.account=%(account)s AND c.chat_type='private' AND c.purged_at IS NULL
    AND c.provider_chat ~ '^[1-9][0-9]{{0,19}}$' AND c.provider_chat<>%(service)s
    AND c.provider_chat IS DISTINCT FROM %(self)s AND NOT {self_chat_sql('c')}
    AND r.outgoing>=%(outgoing)s AND r.incoming>=%(incoming)s AND NOT r.bot
  ORDER BY c.provider_chat'''


def active_contacts(c, account, *, now=None, self_id=None):
    """[{chat, provider_user, name}] of the account's active Telegram users, by provider user ID."""
    now = now or datetime.now(timezone.utc)
    return c.execute(ACTIVE_SQL, {'account': account, 'since': now - timedelta(days=ACTIVE_DAYS),
        'service': SERVICE_CHAT, 'self': self_id, 'outgoing': MIN_OUTGOING, 'incoming': MIN_INCOMING}).fetchall()


def valid_birthday(day, month, year=None):
    """(day, month, year or None) of a real calendar date (29 February counts), else None."""
    if type(day) is not int or type(month) is not int or year is not None and type(year) is not int:
        return None
    try:
        date(2000, month, day)  # a leap year: 29 February is a date
    except ValueError:
        return None
    return day, month, year if year is not None and 1 <= year <= 9999 else None


def occurrence(day, month, today):
    """The next birthday on or after `today`; 29 February falls on 28 February in a year without it."""
    for year in (today.year, today.year + 1):
        try:
            when = date(year, month, day)
        except ValueError:
            when = date(year, 2, 28)
        if when >= today:
            return when
    raise ValueError('invalid_birthday')


def prune(c, account, active):
    """Delete the rows of users who are no longer active (the service-run DELETE ANYM-919 names)."""
    return c.execute('DELETE FROM contact_birthdays WHERE account=%s AND NOT provider_user=ANY(%s)',
        (account, sorted(active))).rowcount


def due(c, account, active, *, now=None):
    """Active users whose birthday is unread or older than 7 days, unread first, within the hourly read budget
    (READS_PER_HOUR minus the rows checked in the last hour, so a restart starts no new budget)."""
    now = now or datetime.now(timezone.utc)
    rows = {r['provider_user']: r['checked_at'] for r in c.execute(
        'SELECT provider_user,checked_at FROM contact_birthdays WHERE account=%s', (account,)).fetchall()}
    spent = sum(1 for at in rows.values() if at > now - timedelta(hours=1))
    stale = [user for user in active if user not in rows or rows[user] <= now - STALE_AFTER]
    stale.sort(key=lambda user: (user in rows, rows.get(user) or now, int(user)))
    return stale[:max(0, READS_PER_HOUR - spent)]


def store(c, account, user, birthday, *, now=None):
    """Write one user's result: a (day, month, year) birthday is known, None is hidden."""
    now = now or datetime.now(timezone.utc)
    day, month, year = birthday if birthday is not None else (None, None, None)
    c.execute('''INSERT INTO contact_birthdays(account,provider_user,day,month,year,state,checked_at)
        VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(account,provider_user) DO UPDATE SET day=excluded.day,
        month=excluded.month,year=excluded.year,state=excluded.state,checked_at=excluded.checked_at''',
        (account, user, day, month, year, 'hidden' if birthday is None else 'known', now))


def upcoming(c, account, today, days_ahead=DAYS_AHEAD, *, chat_scope=None, allowed=None, now=None, self_id=None):
    """list_birthdays: the known birthdays of active contacts within `days_ahead` days of `today`, by date. A
    contact outside the key's chats or the collection policy is neither listed nor counted; hidden or unread
    birthdays are only counted in not_shared."""
    if type(days_ahead) is not int or not 0 <= days_ahead <= MAX_DAYS_AHEAD:
        raise ValueError('invalid_days_ahead')
    known = {r['provider_user']: r for r in c.execute(
        "SELECT provider_user,day,month,year FROM contact_birthdays WHERE account=%s AND state='known'",
        (account,)).fetchall()}
    listed, not_shared = [], 0
    for contact in active_contacts(c, account, now=now, self_id=self_id):
        if chat_scope is not None and contact['chat'] not in chat_scope:
            continue
        if allowed is not None and not allowed(contact['provider_user']):
            continue
        row = known.get(contact['provider_user'])
        birthday = row and valid_birthday(row['day'], row['month'], row['year'])
        if not birthday:
            not_shared += 1
            continue
        day, month, year = birthday
        when = occurrence(day, month, today)
        if (when - today).days > days_ahead:
            continue
        item = {'name': contact['name'], 'chat_id': contact['chat'], 'day': day, 'month': month, 'year': year,
            'date': when.isoformat(), 'days_until': (when - today).days}
        if year is not None and when.year > year:
            item['age_turning'] = when.year - year
        listed.append(item)
    listed.sort(key=lambda item: (item['days_until'], item['chat_id']))
    return {'outcome': 'confirmed', 'today': today.isoformat(), 'days_ahead': days_ahead,
        'birthdays': listed[:MAX_LISTED], 'truncated': len(listed) > MAX_LISTED, 'not_shared': not_shared,
        'source': 'active_telegram_contacts', 'calendar': 'not_written'}


# Under the pool's 5 s statement_timeout, as the source.health journal (subscriptions.journal_source_health).
HINT_TRANSACTION_TIMEOUT = '2s'
HINT_FUNCTION = 'public.journal_contact_birthdays(text)'


def hint_date(now, zone):
    """The birthday date the contact.birthday hint is due for at `now`: tomorrow in the owner's zone, from 09:00
    local time on; None before 09:00."""
    local = now.astimezone(zone)
    return local.date() + timedelta(days=1) if local.hour >= HINT_HOUR else None


def journal_birthday_hints(store, *, now=None, environ=None):
    """The matcher's call of schema 89's definer function with the owner's zone; the function derives tomorrow's date
    and the 09:00 gate from the database clock. None while that function is absent or not granted (schema 86 ships the
    table first), and never raises."""
    import os
    from .time_references import owner_zone
    zone, name = owner_zone(os.environ if environ is None else environ)
    if hint_date(now or datetime.now(timezone.utc), zone) is None:
        return 0  # before 09:00 owner time: no database call
    name = 'UTC' if name == 'UTC_fallback' else name
    try:
        with store.pool.connection() as c:
            c.execute("SELECT set_config('transaction_timeout',%s,true)", (HINT_TRANSACTION_TIMEOUT,))
            c.execute("SET LOCAL lock_timeout='500ms'")
            if not c.execute('''SELECT coalesce(has_function_privilege(to_regprocedure(%s),'EXECUTE'),false)
                    AS ready''', (HINT_FUNCTION,)).fetchone()['ready']:
                return None
            return c.execute('SELECT public.journal_contact_birthdays(%s) AS n', (name,)).fetchone()['n']
    except Exception as error:
        from .telemetry import capture
        capture(error, 'birthday_hint_failed')
        print(json.dumps({'error': 'birthday_hint_failed', 'class': type(error).__name__}), flush=True)
        return None
