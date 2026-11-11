"""Telegram birthdays (ANYM-919): the collector's bounded birthday pass and the list_birthdays read.

Every 10 minutes the pass takes the account's active contacts (birthdays.py) the collection policy allows, deletes
the stored row of anyone no longer among them, reads `contacts.getBirthdays` (no arguments; Telegram answers only for
birthdays within a day of today) at most every 6 hours, then reads `users.getFullUser` for each active user whose
birthday is unread or older than 7 days, at most 30 per hour per account (counted from the stored check times, so a
restart starts no new budget). Each read is one governed BACKGROUND request: a FloodWait stops the pass and the next
one starts only after the wait. A user who shares no birthday, or whom Telegram refuses, is stored `hidden`.
Nothing is sent, and no calendar is written. Logs carry counts only. ANYMSG_TELEGRAM_BIRTHDAYS=0 stops the reads
(the stored rows stay until it is on again).
"""
import asyncio
from contextlib import suppress
import json
import logging
import os
import time

from . import birthdays
from .telegram_governor import BACKGROUND, ProviderRateLimited

PASS_INTERVAL = 600
CONTACT_BIRTHDAYS_EVERY = 6 * 3600
# One governed read holds the shared background lane at most this long.
READ_TIMEOUT = 30
log = logging.getLogger(__name__)


def telegram_birthdays():
    """The birthday reads, on unless ANYMSG_TELEGRAM_BIRTHDAYS=0."""
    return os.environ.get('ANYMSG_TELEGRAM_BIRTHDAYS') != '0'


def birthday_value(birthday):
    """(day, month, year or None) of a Telethon Birthday, or None when absent or not a calendar date."""
    if birthday is None:
        return None
    return birthdays.valid_birthday(getattr(birthday, 'day', None), getattr(birthday, 'month', None),
        getattr(birthday, 'year', None))


class BirthdayProvider:
    """TelethonProvider's two birthday reads."""

    async def full_user_birthday(self, user_id):
        """One user's birthday, or None when the user shares none or Telegram refuses the read. A FloodWait raises
        ProviderRateLimited (through the governor); a read longer than READ_TIMEOUT raises TimeoutError."""
        from telethon import functions
        from telethon.errors import FloodError, FloodWaitError, RPCError

        async def read():
            try:
                async with asyncio.timeout(READ_TIMEOUT):
                    peer = await self.client.get_input_entity(int(user_id))
                    return await self.client(functions.users.GetFullUserRequest(peer))
            except (FloodWaitError, FloodError, TimeoutError):
                raise
            except (RPCError, ValueError, TypeError):
                return None
        full = await self.rpc(read, 'telegram_birthday', tier=BACKGROUND)
        return birthday_value(getattr(getattr(full, 'full_user', None), 'birthday', None))

    async def contact_birthdays(self):
        """{provider user ID: (day, month, year or None)} from contacts.getBirthdays."""
        from telethon import functions

        async def read():
            async with asyncio.timeout(READ_TIMEOUT):
                return await self.client(functions.contacts.GetBirthdaysRequest())
        result = await self.rpc(read, 'telegram_birthdays', tier=BACKGROUND)
        found = {}
        for item in getattr(result, 'contacts', None) or []:
            value = birthday_value(getattr(item, 'birthday', None))
            if type(getattr(item, 'contact_id', None)) is int and value is not None:
                found[str(item.contact_id)] = value
        return found


class BirthdayCollector:
    """TelegramCollector's birthday pass and list_birthdays."""
    birthdays_interval = PASS_INTERVAL

    async def birthdays_loop(self):
        """Never ends the collector: a failed pass is logged by class; a FloodWait defers the next pass past it."""
        while not self.stopping.is_set():
            wait = 0
            try:
                wait = await self.birthdays_pass()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                with suppress(Exception):
                    log.warning('birthdays_failed: %s', type(exc).__name__)
            with suppress(TimeoutError):
                async with asyncio.timeout(max(self.birthdays_interval, wait or 0)):
                    await self.stopping.wait()

    def birthday_allowed(self, user):
        policy = self.policy
        return bool(policy) and policy.get('enabled', True) is not False and self.state.allows(policy, user)

    def birthday_self_id(self):
        read = getattr(self.provider, 'self_provider_id', None)
        return read() if callable(read) else None

    def birthday_plan(self, self_id):
        """(active users, users due for a read, rows deleted); the deletion is fenced by the lease."""
        st = self.state
        with st.archive.store.pool.connection() as c:
            active = [r['provider_user'] for r in birthdays.active_contacts(c, st.account, self_id=self_id)
                      if self.birthday_allowed(r['provider_user'])]
        with st.archive.store.pool.connection() as c:
            st.archive.fence(c, st.account, st.lease)
            pruned = birthdays.prune(c, st.account, active)
            return set(active), birthdays.due(c, st.account, active), pruned

    def birthday_write(self, results):
        st = self.state
        with st.archive.store.pool.connection() as c:
            st.archive.fence(c, st.account, st.lease)
            for user, birthday in results.items():
                birthdays.store(c, st.account, user, birthday)

    async def birthdays_pass(self):
        """One pass; returns the FloodWait seconds that stopped it, else 0."""
        if not telegram_birthdays():
            return 0
        active, users, pruned = await asyncio.to_thread(self.birthday_plan, self.birthday_self_id())
        counts = {'active': len(active), 'pruned': pruned, 'read': 0, 'known': 0, 'hidden': 0}
        try:
            last = self.__dict__.get('_contact_birthdays_at')
            if hasattr(self.provider, 'contact_birthdays') and (
                    last is None or time.monotonic() - last >= CONTACT_BIRTHDAYS_EVERY):
                self._contact_birthdays_at = time.monotonic()
                try:
                    found = await self.provider.contact_birthdays()
                except (ProviderRateLimited, TimeoutError):
                    raise
                except Exception as exc:
                    found = {}
                    with suppress(Exception):
                        log.warning('contact_birthdays_failed: %s', type(exc).__name__)
                known = {user: value for user, value in found.items() if user in active}
                if known:
                    await asyncio.to_thread(self.birthday_write, known)
                    counts['known'] += len(known)
                    users = [user for user in users if user not in known]  # fresh now: no full-user read
            for user in users:
                if self.stopping.is_set() or self.failed.is_set():
                    break
                value = await self.provider.full_user_birthday(user)
                counts['read'] += 1
                counts['known' if value is not None else 'hidden'] += 1
                await asyncio.to_thread(self.birthday_write, {user: value})
        except ProviderRateLimited as exc:
            counts['rate_limited'] = exc.seconds
            return exc.seconds
        except TimeoutError:
            counts['timeout'] = 1
            return 0
        finally:
            with suppress(Exception):
                print(json.dumps({'event': 'telegram_birthdays', **counts}, sort_keys=True), flush=True)
        return 0

    async def list_birthdays(self, job):
        return await asyncio.to_thread(self.birthday_list, job)

    def birthday_list(self, job):
        """list_birthdays from the archive: no provider request. Scoped by the key's chats, read now, and the
        collection policy the dispatch read; dated in the owner's zone (ANYMSG_OWNER_TIMEZONE)."""
        from datetime import datetime
        from .commands import CommandRejected
        from .time_references import owner_zone
        policy = job.get('_policy')
        if (not isinstance(policy, dict) or policy.get('enabled') is False or
                type(policy.get('discover_chats')) is not bool or
                not isinstance(policy.get('include_chats'), (list, tuple)) or
                not isinstance(policy.get('exclude_chats'), (list, tuple))):
            raise CommandRejected('operation_access_changed')
        zone, label = owner_zone(os.environ)
        days = (job.get('parameters') or {}).get('days_ahead', birthdays.DAYS_AHEAD)
        with self.state.archive.store.pool.connection() as c:
            key = c.execute('SELECT chat_ids FROM principals WHERE id=%s AND enabled', (job.get('principal'),)).fetchone()
            if not key or job.get('account') != self.state.account:
                raise CommandRejected('operation_access_changed')
            try:
                result = birthdays.upcoming(c, self.state.account, datetime.now(zone).date(), days,
                    chat_scope=key['chat_ids'], allowed=lambda user: self.state.allows(policy, user),
                    self_id=self.birthday_self_id())
            except ValueError:
                raise CommandRejected('invalid_parameters') from None
        outcome = result.pop('outcome')
        return {'outcome': outcome, 'data': {**result, 'time_zone': label}}
