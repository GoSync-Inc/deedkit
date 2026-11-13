"""USB source framing for an already authenticated, serial-pinned ADB forward.

The caller owns the connection, reconnect policy and existing service lifecycle.
This helper neither invokes ADB nor starts another monitor or native client.
"""
import json

from .order_events import Notification, OrderRefused, document


class UsbOrderSource:
    def __init__(self, events, *, principal, owner, device_id, source_id, auth_epoch):
        self.events = events
        self.principal, self.owner, self.device_id = principal, owner, device_id
        self.source_id, self.auth_epoch = source_id, auth_epoch
        events.db.execute('''CREATE TABLE IF NOT EXISTS android_order_source_cursors
            (owner TEXT,device_id TEXT,source_id TEXT,epoch TEXT,sequence INTEGER,hold_reason TEXT,
             PRIMARY KEY(owner,device_id,source_id))''')
        events.db.commit()
        self.epoch = None

    def status(self):
        row = self.events.db.execute('''SELECT epoch,sequence,hold_reason FROM android_order_source_cursors
            WHERE owner=? AND device_id=? AND source_id=?''', (self.owner, self.device_id, self.source_id)).fetchone()
        return dict(row) if row else {'epoch': None, 'sequence': 0, 'hold_reason': 'source_not_connected'}

    def watch(self, packages):
        status = self.status()
        return {'version': 1, 'packages': sorted(set(packages)), 'source_epoch': status['epoch'],
                'after': status['sequence']}

    def _cursor(self, epoch, sequence, reason):
        with self.events.tx():
            self.events.db.execute('''INSERT INTO android_order_source_cursors VALUES(?,?,?,?,?,?)
                ON CONFLICT(owner,device_id,source_id) DO UPDATE SET epoch=excluded.epoch,
                sequence=excluded.sequence,hold_reason=excluded.hold_reason''',
                (self.owner, self.device_id, self.source_id, epoch, sequence, reason))

    def consume(self, frame):
        if not isinstance(frame, dict) or len(document(frame).encode()) > 4096:
            raise OrderRefused('invalid_source_frame')
        kind, epoch = frame.get('type'), frame.get('source_epoch')
        if not isinstance(epoch, str) or not 1 <= len(epoch) <= 64:
            raise OrderRefused('invalid_source_epoch')
        if kind == 'hello':
            sequence, oldest = frame.get('last_sequence'), frame.get('oldest_sequence')
            if (frame.get('version') != 1 or type(sequence) is not int or sequence < 0
                    or type(oldest) is not int or oldest < 1 or oldest > sequence + 1):
                raise OrderRefused('invalid_source_cursor')
            previous = self.status()
            changed = previous['epoch'] != epoch
            gap = changed or frame.get('gap') is not False or previous['sequence'] < oldest - 1
            self.epoch = epoch
            self._cursor(epoch, oldest - 1 if changed else previous['sequence'],
                         'observation_gap' if gap else previous['hold_reason'])
            return 'connected'
        if epoch != self.epoch:
            raise OrderRefused('source_not_connected')
        if kind == 'heartbeat':
            return 'heartbeat'
        if kind != 'observation':
            raise OrderRefused('unsupported_source_frame')
        sequence = frame.get('sequence')
        if type(sequence) is not int or sequence < 1:
            raise OrderRefused('invalid_source_cursor')
        status = self.status()
        if sequence <= status['sequence']:
            return 'duplicate'
        # Correlation remains explicitly app-level. Core may inspect the bound active orders.
        observation = Notification(event_id=f'{epoch}:{sequence}', owner=self.owner, device_id=self.device_id,
            order_id=None, source_order_id=None, package=frame.get('package'), source_id=self.source_id,
            auth_epoch=self.auth_epoch, observed_at=frame.get('observed_at'),
            observation_ref=frame.get('observation_ref'))
        try:
            outcome = self.events.observe(self.principal, observation)
        except OrderRefused as error:
            if str(error) not in ('stale_observation', 'order_not_bound'):
                raise
            outcome = str(error)
        # Commit observation first. A crash before cursor commit safely replays its exact event identity.
        self._cursor(epoch, sequence, status['hold_reason'])
        return outcome

    async def read_stream(self, reader, writer, packages):
        writer.write((document(self.watch(packages)) + '\n').encode())
        await writer.drain()
        try:
            while raw := await reader.readline():
                if len(raw) > 4096 or not raw.endswith(b'\n'):
                    raise OrderRefused('invalid_source_frame')
                try:
                    frame = json.loads(raw)
                except ValueError:
                    raise OrderRefused('invalid_source_frame') from None
                self.consume(frame)
        finally:
            status = self.status()
            self._cursor(status['epoch'], status['sequence'], 'source_disconnected')
            writer.close()
