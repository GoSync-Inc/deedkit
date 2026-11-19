"""The sender repair takes no external action while it runs (ANYM-187 criterion 5).

Existing evidence is structural: repair_sent_senders takes the archive, account, lease and verified owner, and no
provider client; one test also sees the outbox row count unchanged. This module adds a behavioural check scoped to the
repair call only. Reversible tripwires refuse, before anything happens, every way it could:
- reach Telegram (client construction, connect, start, request, send);
- open a network connection (socket, asyncio, httpx, urllib);
- start a process or browser;
- enqueue or dispatch an outgoing message through the API.
The database rows that carry outgoing requests, their receipts, commands and the collector lease and session claims
are compared around the call. The repair itself must still happen. Database access is libpq, not Python sockets.
Limits: the tripwires cover these named Python entry points, not every conceivable syscall.
"""
import asyncio
from contextlib import contextmanager
from copy import deepcopy
import os
import socket
import subprocess
import urllib.request
import webbrowser

import httpx
import pytest
import telethon
from telethon import types

from messaging_service.outgoing import OutgoingWorker
from messaging_service.store import Store
from messaging_service.telegram_identity import repair_sent_senders
from test_archive import event
from test_sending import sender as sender, submit  # noqa: F401 (sender is a fixture)

OWNER = types.User(id=42, is_self=True, first_name='Synthetic', last_name='Owner', username='fixture_owner')


class ForbiddenAction(Exception):
    """A tripwire refused an external action before it happened."""


def targets():
    """(owner, attribute, label) for every entry point the repair must never reach."""
    return [
        (telethon.TelegramClient, '__init__', 'telegram.client'), (telethon.TelegramClient, 'connect', 'telegram.connect'),
        (telethon.TelegramClient, 'start', 'telegram.start'), (telethon.TelegramClient, '__call__', 'telegram.request'),
        (telethon.TelegramClient, 'send_message', 'telegram.send_message'),
        (socket.socket, 'connect', 'network.socket'), (socket.socket, 'connect_ex', 'network.socket_ex'),
        (socket, 'create_connection', 'network.create_connection'), (asyncio, 'open_connection', 'network.asyncio'),
        (httpx.Client, 'send', 'network.httpx'), (httpx.AsyncClient, 'send', 'network.httpx_async'),
        (urllib.request, 'urlopen', 'network.urllib'),
        (subprocess.Popen, '__init__', 'process.popen'), (os, 'system', 'process.system'),
        (os, 'posix_spawn', 'process.spawn'), (webbrowser, 'open', 'process.browser'),
        (Store, 'send_message', 'outgoing.send_message'),
        (Store, 'execute_messenger_operation', 'outgoing.messenger_operation'),
        (OutgoingWorker, '__init__', 'outgoing.dispatch_worker'), (OutgoingWorker, 'once', 'outgoing.dispatch'),
    ]


@contextmanager
def tripwires(monkeypatch):
    attempts = []

    def refuse(label):
        def tripped(*args, **kwargs):
            attempts.append(label)
            raise ForbiddenAction(label)
        return tripped
    with monkeypatch.context() as patch:
        for owner, attribute, label in targets():
            patch.setattr(owner, attribute, refuse(label))
        yield attempts


def records(store, account):
    """Everything an external action would leave behind: outgoing requests and receipts, commands, lease, sessions."""
    with store.pool.connection() as c:
        return deepcopy({
            'outgoing': c.execute('SELECT * FROM outgoing_messages WHERE account=%s ORDER BY id', (account,)).fetchall(),
            'commands': c.execute('SELECT * FROM messenger_commands WHERE account=%s ORDER BY id', (account,)).fetchall(),
            'lease': c.execute('SELECT * FROM collector_leases WHERE account=%s', (account,)).fetchall(),
            'sessions': c.execute('SELECT * FROM session_claims WHERE account=%s', (account,)).fetchall()})


@pytest.fixture()
def confirmed(sender):  # noqa: F811
    """A confirmed private short send whose archived copy has an empty sender: exactly one repair is due."""
    s, archive, key, _, state = sender
    submit(s, key)
    async def send(job): return {'provider_message_id': '42'}
    asyncio.run(OutgoingWorker(state, send).once())
    mid = archive.ingest(event(account='send-fixture', provider_id='42', sender='', body='Exact synthetic text',
                               direction='outgoing', metadata={'chat_type': 'private'}))['message']
    return s, archive, key, state, mid


# spec: ANYM-187
def test_every_tripwire_refuses_before_acting_and_is_removed_afterwards(monkeypatch):
    originals = {(owner, attribute): getattr(owner, attribute) for owner, attribute, _ in targets()}
    calls = [
        ('telegram.client', lambda: telethon.TelegramClient('synthetic-session', 1, 'f' * 32)),
        ('network.socket', lambda: socket.socket().connect(('192.0.2.1', 9))),
        ('network.create_connection', lambda: socket.create_connection(('192.0.2.1', 9), timeout=0.01)),
        ('network.asyncio', lambda: asyncio.run(asyncio.open_connection('192.0.2.1', 9))),
        ('network.httpx', lambda: httpx.Client().get('https://synthetic.invalid/')),
        ('network.urllib', lambda: urllib.request.urlopen('https://synthetic.invalid/')),
        ('process.popen', lambda: subprocess.Popen(['true'])),
        ('process.system', lambda: os.system('true')),
        ('process.browser', lambda: webbrowser.open('about:blank')),
        ('outgoing.send_message', lambda: Store.send_message(None, 'synthetic-key')),
        ('outgoing.dispatch_worker', lambda: OutgoingWorker(None, None)),
    ]
    with tripwires(monkeypatch) as attempts:
        for label, call in calls:
            with pytest.raises(ForbiddenAction, match=f'^{label}$'):
                call()
        assert attempts == [label for label, _ in calls]
    assert {key: getattr(*key) for key in originals} == originals  # nothing stays patched


# spec: ANYM-187
def test_the_repair_completes_with_no_provider_network_process_or_outgoing_attempt(confirmed, monkeypatch):
    s, archive, key, state, mid = confirmed
    before = records(s, 'send-fixture')
    assert len(before['outgoing']) == 1 and before['outgoing'][0]['status'] == 'sent' and before['lease']
    with tripwires(monkeypatch) as attempts:
        repaired = repair_sent_senders(archive, 'send-fixture', state.lease, OWNER)
    assert attempts == [] and repaired == 1
    # The repair happened: a new reconcile revision on the authenticated owner.
    message = s.get_context(key, mid)['message']
    assert (message['sender'], message['revision']) == ('42', 2)
    # Nothing an external action would leave changed: requests, receipts, commands, lease and session claims.
    assert records(s, 'send-fixture') == before
