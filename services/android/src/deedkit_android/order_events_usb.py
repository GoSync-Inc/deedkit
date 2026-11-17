"""Same-process USB lifecycle connector; trusted forward acquisition belongs to the host.

No ADB invocation, lease acquisition, native client, order policy or notification text.
The host's admit closure must retain the exact original live authority generation.
"""
import asyncio
from dataclasses import dataclass
import inspect
import json
import re
import threading

from .order_events import OrderRefused
from .order_events_source import UsbOrderSource


@dataclass(frozen=True, repr=False)
class SourceAccess:
    principal: object
    serial: str


@dataclass(frozen=True, repr=False)
class UsbForward:
    serial: str
    port: int
    close_owned: object
    receipt: object  # host-private original acquisition receipt; never projected/logged
    collection: object = None  # host journal hook: 'watching' before watch bytes, 'accepted' after its hello


class _Hold(Exception):
    pass


# One service process already owns the Store. This gate also spans owners/connectors
# inside it; it is not restart authority. Unknown acquisition retains the slot.
_SERIALS = {}
_SERIAL_LOCK = threading.Lock()
_PACKAGE = re.compile(r'[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+')


class _Reader:
    def __init__(self, connector, reader, access, packages):
        self.connector, self.reader = connector, reader
        self.access, self.packages = access, packages
        self.pending = None

    async def readline(self):
        c = self.connector
        c._remember_gap()
        c._guard(self.access, self.packages)
        self.pending = asyncio.create_task(self.reader.readline())
        try:
            while True:
                done, _ = await asyncio.wait((self.pending,), timeout=c.interval_s)
                c._guard(self.access, self.packages)
                if done:
                    return self.pending.result()
        finally:
            if not self.pending.done():
                self.pending.cancel()
            await asyncio.gather(self.pending, return_exceptions=True)
            self.pending = None


class _Writer:
    def __init__(self, connector, writer, access, packages):
        self.connector, self.writer = connector, writer
        self.access, self.packages = access, packages

    def write(self, raw):
        self.connector._guard(self.access, self.packages)
        self.writer.write(raw)

    async def drain(self):
        self.connector._guard(self.access, self.packages)
        await self.writer.drain()
        self.connector._guard(self.access, self.packages)

    def close(self):
        self.writer.close()


class UsbOrderConnector:
    """A supplies fresh admit() and a proved-owned forward; start/close live in A's lifecycle.

    bind_usb must itself durably reconcile uncertain acquisition across host restart.
    No automatic second acquisition occurs here, even after a disconnect.
    """
    def __init__(self, events, *, owner, device_id, source_id, auth_epoch, admit, bind_usb,
                 interval_s=1, reconnect_s=2, cleanup_s=40, attempts=3):
        if (not callable(admit) or not callable(bind_usb) or interval_s <= 0 or reconnect_s <= 0
                or not 0 < cleanup_s <= 60 or type(attempts) is not int or not 1 <= attempts <= 8):
            raise ValueError('invalid_usb_connector')
        self.events, self.owner, self.device_id = events, owner, device_id
        self.source_id, self.auth_epoch = source_id, auth_epoch
        self.admit, self.bind_usb = admit, bind_usb
        self.interval_s, self.reconnect_s, self.cleanup_s, self.attempts = interval_s, reconnect_s, cleanup_s, attempts
        self.source = UsbOrderSource(events, principal=None, owner=owner, device_id=device_id,
                                     source_id=source_id, auth_epoch=auth_epoch)
        self.source.accepted = lambda: self._collection('accepted')
        self.task = self.forward = self.writer = self.access = None
        self._unproved_forward = None
        self.serial = None
        self.closing = False
        self.state, self.reason, self.cleanup = 'idle', 'not_started', 'not_needed'
        self.gap = self.source.status()['hold_reason'] == 'observation_gap'
        self._uncertain = False
        self._claimed = False
        self._close_requested = False
        self._finishing = False

    def status(self):
        return {'state': self.state, 'reason': self.reason, 'cleanup': self.cleanup,
                'observation_gap': self.gap, 'cursor': self.source.status()['sequence']}

    def _access(self):
        try:
            access = self.admit()
            if (not isinstance(access, SourceAccess) or getattr(access.principal, 'id', None) != self.owner
                    or not isinstance(access.serial, str) or not access.serial):
                raise _Hold('source_access_changed')
        except _Hold:
            raise
        except Exception:
            raise _Hold('source_access_changed') from None
        return access

    def _packages(self, access):
        rows = self.events.db.execute('''SELECT * FROM android_order_obligations WHERE owner=?
            AND device_id=? AND source_id=? AND auth_epoch=? AND lifecycle='active' ''',
            (self.owner, self.device_id, self.source_id, self.auth_epoch)).fetchall()
        packages = set()
        for raw in rows:
            row = dict(raw)
            if row.get('task_only') or row['package'] is None:
                continue
            try:
                self.events._authorize(access.principal, row)
            except OrderRefused:
                raise _Hold('source_access_changed') from None
            package = row['package']
            if len(package) > 200 or not _PACKAGE.fullmatch(package):
                raise _Hold('invalid_watch_package')
            packages.add(package)
        if len(packages) > 32:
            raise _Hold('watch_package_limit')
        return frozenset(packages)

    def _guard(self, original, packages=None):
        current = self._access()
        if current.serial != original.serial:
            raise _Hold('source_access_changed')
        self.source.principal = current.principal
        if packages is not None and self._packages(current) != packages:
            raise _Hold('watch_changed')
        return current

    def _collection(self, event):
        if self.forward is not None and callable(self.forward.collection):
            self.forward.collection(event)

    def _remember_gap(self):
        self.gap |= self.source.status()['hold_reason'] == 'observation_gap'

    def _hold(self, reason):
        self.state, self.reason = 'held', reason
        status = self.source.status()
        self.source._cursor(status['epoch'], status['sequence'], 'observation_gap' if self.gap else reason)

    def _claim(self, serial):
        with _SERIAL_LOCK:
            if serial in _SERIALS:
                raise _Hold('serial_subscriber_busy')
            _SERIALS[serial] = self
        self.serial, self._claimed = serial, True

    def _release(self):
        if self._claimed and not self._uncertain:
            with _SERIAL_LOCK:
                if _SERIALS.get(self.serial) is self:
                    del _SERIALS[self.serial]
            self._claimed = False

    def start(self):
        if self.task is not None or self.closing:
            raise OrderRefused('usb_connector_already_started')
        self.task = asyncio.create_task(self._run())

    async def _connect(self, access):
        self._guard(access)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection('127.0.0.1', self.forward.port, limit=4097), self.cleanup_s)
        self.writer = writer  # retain ownership before the post-await authority check
        self._guard(access)
        return reader, writer

    async def _close_writer(self):
        writer, self.writer = self.writer, None
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), self.cleanup_s)
            except (OSError, TimeoutError):
                pass  # closed host admission; device watch still needs a positive emptywatch hello

    async def _emptywatch(self):
        """Only an authorized, proved-owned capability; one bounded cleanup exchange."""
        self._guard(self.access)
        reader, writer = await self._connect(self.access)
        try:
            writer.write((json.dumps(self.source.watch([])) + '\n').encode())
            await writer.drain()
            self._guard(self.access)
            # Existing device heartbeat may delay accepting the new client for 30 seconds.
            raw = await _Reader(self, reader, self.access, None).readline()
            if len(raw) > 4096 or not raw.endswith(b'\n'):
                raise _Hold('cleanup_unconfirmed')
            frame = json.loads(raw)
            if (frame.get('type') != 'hello' or frame.get('version') != 1
                    or not isinstance(frame.get('source_epoch'), str)
                    or not frame['source_epoch']
                    or type(frame.get('last_sequence')) is not int or frame['last_sequence'] < 0
                    or type(frame.get('oldest_sequence')) is not int
                    or not 1 <= frame['oldest_sequence'] <= frame['last_sequence'] + 1
                    or type(frame.get('gap')) is not bool):
                raise _Hold('cleanup_unconfirmed')
        finally:
            await self._close_writer()

    async def _finish(self):
        # Reading has ended. Undo keeps the original key/session checks but must not need a retained run.
        self.closing = self._finishing = True
        self._remember_gap()
        deadline = asyncio.get_running_loop().time() + self.cleanup_s
        try:
            async with asyncio.timeout_at(deadline):
                await self._close_writer()
                if self.forward is not None:
                    self.cleanup = 'cleanup_unconfirmed'
                    try:
                        # Reserve a bounded portion for closing the proved-owned forward.
                        async with asyncio.timeout(max(.001, self.cleanup_s * .8)):
                            await self._emptywatch()
                        self.cleanup = 'confirmed'
                    except Exception:
                        pass
                    result = self.forward.close_owned()
                    if inspect.isawaitable(result):
                        result = await result
                    if result is not True:
                        raise _Hold('forward_cleanup_unknown')
                    self.forward = None
        except (Exception, asyncio.CancelledError):
            if self.forward is not None:
                self._uncertain = True
                self.reason = 'forward_cleanup_unknown'
        self._release()
        self._hold(self.reason or 'source_stopped')

    async def _run(self):
        self.state, self.reason = 'starting', None
        try:
            self.access = self._access()
            packages = self._packages(self.access)
            if not packages:
                raise _Hold('no_active_packages')
            self._claim(self.access.serial)
            self.cleanup = 'cleanup_unconfirmed'
            try:
                self.forward = await asyncio.wait_for(self.bind_usb(self.access), self.cleanup_s)
            except BaseException:
                self._uncertain = True
                raise _Hold('forward_acquisition_unknown') from None
            if (not isinstance(self.forward, UsbForward) or self.forward.serial != self.serial
                    or type(self.forward.port) is not int or not 1 <= self.forward.port <= 65535
                    or not callable(self.forward.close_owned) or self.forward.receipt is None):
                self._unproved_forward = self.forward  # private quarantine; no authority to close it
                self.forward = None
                self._uncertain = True
                raise _Hold('forward_acquisition_unknown')
            self._guard(self.access, packages)
            for attempt in range(self.attempts):
                try:
                    packages = self._packages(self._guard(self.access))
                    if not packages:
                        raise _Hold('no_active_packages')
                    reader, writer = await self._connect(self.access)
                    self._guard(self.access, packages)
                    self._collection('watching')
                    self.state, self.reason = 'reading', None
                    await self.source.read_stream(_Reader(self, reader, self.access, packages),
                                                  _Writer(self, writer, self.access, packages), packages)
                    self._remember_gap()
                    self.reason = 'source_disconnected'
                except _Hold as error:
                    self.reason = str(error)
                    if self.reason != 'watch_changed':
                        raise
                except (OSError, TimeoutError):
                    self.reason = 'source_disconnected'
                await self._close_writer()
                if self.closing:
                    break
                await asyncio.sleep(self.reconnect_s)
                self._guard(self.access)
            else:
                self.reason = 'source_reconnect_exhausted'
        except _Hold as error:
            self.reason = str(error)
        except asyncio.CancelledError:
            self.reason = self.reason or 'source_stopped'
        except Exception:
            self.reason = 'invalid_source_stream'
        finally:
            await self._finish()

    async def close(self):
        self.closing = True
        if self.task is not None:
            if not self._close_requested and not self.task.done():
                self._close_requested = True
                if not self._finishing:
                    self.task.cancel()
            try:
                await asyncio.shield(self.task)
            except asyncio.CancelledError:
                if self.task.cancelled():
                    self._hold('source_stopped')
                else:
                    raise
        else:
            self._hold('source_stopped')
