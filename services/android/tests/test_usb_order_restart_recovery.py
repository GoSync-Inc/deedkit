"""Restart and USB-loss reconciliation of host-owned order-event forwards.

Synthetic ADB forward table and phone listener only. The phone keeps its last watched package set
across host restart and USB loss, like the real listener's persisted SharedPreferences.
"""

import asyncio
import functools
import json
import socket
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from agent_harness import Service
from test_order_supervision import host, rows, until

REMOTE = 'localabstract:deedkit-order-events'


class Phone:
    def __init__(self):
        self.packages, self.watches, self.listener = [], [], None
        self.journal = [dict(type='observation', source_epoch='epoch', sequence=1, package='app.food',
                             observed_at=time.time(), observation_ref='metadata-only')]

    def plug(self):
        self.listener = Listener(self)
        return self.listener

    def unplug(self):
        listener, self.listener = self.listener, None
        listener.close()


class Listener:
    """One USB transport generation. Every nonempty watch replays the whole journal (duplicates)."""
    def __init__(self, phone):
        self.phone, self.closed, self.clients = phone, False, []
        self.socket = socket.socket()
        self.socket.bind(('127.0.0.1', 0))
        self.socket.listen()
        self.socket.settimeout(.1)
        self.port = self.socket.getsockname()[1]
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while not self.closed:
            try:
                client, _ = self.socket.accept()
            except (socket.timeout, OSError):
                continue
            self.clients.append(client)
            stream = None
            try:
                stream = client.makefile('rwb')
                raw = stream.readline()
                if not raw:
                    continue
                watch = json.loads(raw)
                self.phone.packages = list(watch['packages'])  # persisted before hello
                self.phone.watches.append(watch)
                stream.write((json.dumps(dict(type='hello', version=1, source_epoch='epoch', oldest_sequence=1,
                    last_sequence=len(self.phone.journal), gap=False)) + '\n').encode())
                for event in self.phone.journal:
                    if event['package'] in watch['packages']:
                        stream.write((json.dumps(event) + '\n').encode())
                stream.flush()
                stream.read()
            except (OSError, ValueError):
                pass
            finally:
                if stream is not None:
                    stream.close()
                client.close()

    def close(self):
        self.closed = True
        for client in self.clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            client.close()
        self.socket.close()
        self.thread.join(2)
        assert not self.thread.is_alive()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    from deedkit_android import order_usb_host
    config, record = host(tmp_path, monkeypatch, delay=120)
    body = json.loads(config.read_text())
    serial = body['devices']['home'] = 'serial-' + tmp_path.name
    config.write_text(json.dumps(body))
    binary = __import__('pathlib').Path(body['engines']['codex']['binary'])
    binary.write_text(binary.read_text().replace('payload=dict(next_check_at=time.time()+',
        "payload=dict(package='app.food',next_check_at=time.time()+"))
    monkeypatch.setattr(order_usb_host, 'UsbOrderConnector',
                        functools.partial(order_usb_host.UsbOrderConnector, reconnect_s=.05, cleanup_s=3))
    phone = Phone()
    phone.plug()
    calls, table, hang, gate, last = [], [], [None], threading.Event(), [None]

    async def wait_gate():
        while not gate.is_set():
            await asyncio.sleep(.01)

    async def command(argv, timeout, **kwargs):
        calls.append(list(argv))
        previous, last[0] = last[0], argv[-1]
        if argv[-1] == '--list':
            if hang[0] == 'verify' and previous == REMOTE:
                await wait_gate()
            return 0, ('\n'.join(' '.join(r) for r in table) + '\n').encode(), b''
        if '--remove' in argv:
            if hang[0] == 'remove':
                await wait_gate()
            table[:] = [r for r in table if r[1] != argv[-1]]
            return 0, b'', b''
        assert argv[1:3] == ['-s', serial] and '--no-rebind' in argv
        if phone.listener is None:
            return 1, b'', b''
        if hang[0] == 'acquire':
            await wait_gate()
        table.append((serial, 'tcp:' + str(phone.listener.port), REMOTE))
        return 0, str(phone.listener.port).encode(), b''
    monkeypatch.setattr(order_usb_host, 'run_process', command)
    yield SimpleNamespace(config=config, record=record, phone=phone, calls=calls, table=table, hang=hang, gate=gate,
                          serial=serial, database=tmp_path / 'state' / 'state.sqlite3')
    if phone.listener is not None:
        phone.unplug()


def forwards(service):
    return rows(service, 'SELECT * FROM android_usb_forwards ORDER BY rowid')


def before_acquisition(rig, start):
    """Commands the restarted host ran before its one new forward acquisition."""
    calls = rig.calls[start:]
    return calls[:next(i for i, c in enumerate(calls) if '--no-rebind' in c)]


def count(rig, flag, start=0):
    return len([c for c in rig.calls[start:] if flag in c])


def observation_wakes(service):
    return rows(service, "SELECT * FROM android_order_wakes WHERE kind='observation'")


def settled_wake(service):
    """The phone's observation reached the original native turn and the device lock is free again."""
    until(lambda: rows(service, "SELECT * FROM android_order_native_inputs WHERE receipt LIKE '%accepted%'"))
    until(lambda: not service.app.state.devices.locks['home'].locked())


def crash(rig, service, ready):
    """Capture the durable receipt, host forward table and phone set at the crash instant."""
    row = until(lambda: next((r for r in forwards(service) if ready(r)), None))
    return dict(row=row, table=list(rig.table), packages=list(rig.phone.packages))


def restore(rig, snapshot):
    """Undo the graceful shutdown's later writes, so the next start sees exactly the crash state."""
    row = snapshot['row']
    with sqlite3.connect(rig.database) as db:
        db.execute('UPDATE android_usb_forwards SET ' + ','.join(k + '=?' for k in row) + ' WHERE id=?',
                   (*row.values(), row['id']))
    rig.table[:] = snapshot['table']
    rig.phone.packages = snapshot['packages']


def first_life(rig, point):
    """One ordinary admitted request that crashes at point; returns (lease, crash snapshot)."""
    rig.hang[0] = {'intent': 'acquire', 'owned': 'verify', 'cleanup': 'remove'}.get(point)
    with Service(rig.config) as service:
        token = service.lease()
        request = service.start(token, goal='native-control')['request_id']
        if point == 'cleanup':
            service.finished(request)
            settled_wake(service)
            release = threading.Thread(target=service.post, args=('session', dict(action='release', device_id='home',
                                                                                     session_id=token)))
            release.start()
            snapshot = crash(rig, service, lambda r: r['state'] == 'closing')
            rig.gate.set()
            release.join(10)
            token = None
        elif point == 'watch':
            journal, rig.phone.journal = rig.phone.journal, []  # no notification yet: the order stays active
            service.finished(request)
            until(lambda: rig.phone.packages)
            snapshot = crash(rig, service, lambda r: r['state'] == 'owned' and r['collection'] == 'unconfirmed')
            rig.phone.journal = journal
        else:
            snapshot = crash(rig, service, lambda r: r['state'] == {'intent': 'sending', 'owned': 'owned'}[point])
            rig.gate.set()
            service.finished(request)
            settled_wake(service)
    rig.gate.clear()
    rig.hang[0] = None
    restore(rig, snapshot)
    return token, snapshot


def second_request(service, token, key='one'):
    token = token or service.lease(key=key)
    return service.finished(service.start(token, goal='native-control', key=key)['request_id'], key=key)


# spec: ANYM-997
@pytest.mark.parametrize('point,leftover,outcome', [
    ('intent', False, 'forward_absent'),
    ('owned', True, 'forward_removed'),
    ('cleanup', False, 'forward_absent'),
])
def test_restart_reconciles_a_positively_closed_forward_then_a_normal_request_collects_once(rig, point, leftover, outcome):
    token, snapshot = first_life(rig, point)
    if point == 'cleanup':
        assert snapshot['row']['collection'] == 'confirmed' and snapshot['packages'] == []
        rig.table.clear()  # the removal took effect; only its reply was lost in the crash
    if point == 'owned':
        assert snapshot['row']['collection'] == 'not_started' and snapshot['table']
    assert bool(rig.table) == leftover
    start = len(rig.calls)
    with Service(rig.config) as service:
        second_request(service, token)
        until(lambda: observation_wakes(service))
        old, new = until(lambda: (lambda r: r if len(r) == 2 and r[1]['state'] == 'owned' else None)(forwards(service)))
        assert old['id'] == snapshot['row']['id'] and old['state'] == 'closed' and old['recovery'] == outcome
        assert old['request_id'] == snapshot['row']['request_id'] and old['port'] == snapshot['row']['port']
        assert old['reason'] == 'restart_reconciliation_required'
        assert count(rig, '--no-rebind', start) == 1
        removed = [c[-1] for c in before_acquisition(rig, start) if '--remove' in c]
        assert removed == (['tcp:' + str(snapshot['row']['port'])] if leftover else [])
        assert rig.phone.packages == ['app.food'] and rig.phone.watches[-1]['packages'] == ['app.food']
        assert len(observation_wakes(service)) == 1  # the phone's replay stays one wake


# spec: ANYM-997
def test_restart_after_watch_acceptance_keeps_unknown_collection_held_until_a_new_watch_hello_supersedes_it(rig):
    token, snapshot = first_life(rig, 'watch')
    assert snapshot['row']['collection'] == 'unconfirmed' and rig.table
    start, watched = len(rig.calls), len(rig.phone.watches)
    assert snapshot['packages'] == ['app.food']
    with Service(rig.config) as service:
        assert observation_wakes(service) == []
        second_request(service, token)
        old, new = until(lambda: (lambda r: r if len(r) == 2 and r[0]['state'] == 'closed' else None)(forwards(service)))
        assert old['recovery'] == 'collection_superseded' and old['collection'] == 'unconfirmed'
        assert old['reason'] == 'restart_reconciliation_required' and new['request_id'] != old['request_id']
        removed = [c[-1] for c in before_acquisition(rig, start) if '--remove' in c]
        assert removed == ['tcp:' + str(snapshot['row']['port'])]
        assert count(rig, '--no-rebind', start) == 1
        assert rig.phone.watches[watched]['packages'] == ['app.food']  # the superseding watch
        settled_wake(service)
        time.sleep(.2)
        woken = [w['order_id'] for w in observation_wakes(service)]
        assert woken and len(woken) == len(set(woken))  # at most one wake per order bound at observation time


# spec: ANYM-997
def test_restart_never_retries_an_attempted_removal_or_guesses_an_unported_forward(rig):
    token, snapshot = first_life(rig, 'cleanup')
    rig.table[:] = snapshot['table'] = [(rig.serial, 'tcp:' + str(snapshot['row']['port']), REMOTE)]
    start = len(rig.calls)
    with Service(rig.config) as service:
        second_request(service, token)
        time.sleep(.3)
        row, = forwards(service)
        assert row['state'] == 'held' and row['recovery'] == 'after_closing'
        assert count(rig, '--remove', start) == count(rig, '--no-rebind', start) == 0
        assert rig.table == snapshot['table']


# spec: ANYM-997
@pytest.mark.parametrize('point', ['intent', 'watch'])
def test_foreign_forward_is_never_adopted_removed_or_used_to_revive_collection(rig, point):
    token, snapshot = first_life(rig, point)
    foreign = (rig.serial, 'tcp:12345', REMOTE)
    rig.table[:] = [foreign]
    start = len(rig.calls)
    with Service(rig.config) as service:
        view = second_request(service, token)
        time.sleep(.3)
        row, = forwards(service)
        assert row['state'] == 'held' and row['id'] == snapshot['row']['id']
        assert row['recovery'] == ('after_sending' if point == 'intent' else 'forward_closed')
        assert count(rig, '--remove', start) == count(rig, '--no-rebind', start) == 0
        assert rig.table == [foreign] and service.get(view['request_id'])['usb_source']['state'] == 'held'
        if point == 'watch':
            assert rig.phone.packages == ['app.food']  # absent own port is not proof the phone stopped


# spec: ANYM-997
@pytest.mark.parametrize('change', ['new_owner', 'new_auth_epoch'])
def test_another_owner_or_access_epoch_cannot_reconcile_or_remove_the_original_forward(rig, change):
    token, snapshot = first_life(rig, 'owned')
    key = 'one'
    if change == 'new_auth_epoch':
        body = json.loads(rig.config.read_text())
        body['agent']['order_events']['auth_epoch'] = 2
        rig.config.write_text(json.dumps(body))
    else:
        key = 'two'
    start = len(rig.calls)
    with Service(rig.config) as service:
        if key == 'two':
            service.post('session', dict(action='release', device_id='home', session_id=token))
            token = None
        second_request(service, token, key=key)
        time.sleep(.3)
        row, = forwards(service)
        assert row['state'] == 'held' and row['recovery'] == 'after_owned'
        assert count(rig, '--remove', start) == count(rig, '--no-rebind', start) == 0
        assert rig.table == snapshot['table']


# spec: ANYM-997
def test_usb_loss_keeps_phone_collection_held_until_reconnected_normal_request_supersedes_it_once(rig):
    journal, rig.phone.journal = rig.phone.journal, []  # the notification arrives while USB is lost
    with Service(rig.config) as service:
        token = service.lease()
        service.finished(service.start(token, goal='native-control')['request_id'])
        until(lambda: rig.phone.packages and forwards(service)[0]['collection'] == 'unconfirmed')
        rig.phone.journal = journal
        rig.phone.unplug()
        rig.table.clear()  # the ADB server drops the transport's forward
        lost, = until(lambda: [r for r in forwards(service) if r['state'] == 'held'])
        assert lost['reason'] == 'collection_cleanup_unconfirmed' and lost['collection'] == 'unconfirmed'
        assert rig.phone.packages == ['app.food']
        rig.phone.plug()
        start = len(rig.calls)
        second_request(service, token)
        old, new = until(lambda: (lambda r: r if len(r) == 2 and r[0]['state'] == 'closed' else None)(forwards(service)))
        assert old['id'] == lost['id'] and old['recovery'] == 'collection_superseded'
        assert old['reason'] == 'collection_cleanup_unconfirmed' and new['id'] != old['id']
        assert count(rig, '--no-rebind', start) >= 1 and not [c for c in before_acquisition(rig, start) if '--remove' in c]
        settled_wake(service)
        time.sleep(.2)
        woken = [w['order_id'] for w in observation_wakes(service)]
        assert woken and len(woken) == len(set(woken))  # at most one wake per order bound at observation time
