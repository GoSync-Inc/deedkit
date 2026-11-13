"""Immutable owner-scoped export for the existing gateway bridge. No delivery client or timer loop."""
from datetime import datetime, timezone
import hashlib
import json
import uuid

from .order_events import OrderRefused, document
from .order_events_control import principal_id


class OrderFeed:
    def __init__(self, control, *, lifetime_s=3600):
        if type(lifetime_s) is not int or not 60 <= lifetime_s <= 86400:
            raise OrderRefused('invalid_feed_lifetime')
        self.control, self.events, self.lifetime_s = control, control.events, lifetime_s
        self.events.db.executescript('''
            CREATE TABLE IF NOT EXISTS android_order_feed_heads
              (owner TEXT,device_id TEXT,journal_id TEXT,sequence INTEGER DEFAULT 0,PRIMARY KEY(owner,device_id));
            CREATE TABLE IF NOT EXISTS android_order_feed
              (owner TEXT,device_id TEXT,sequence INTEGER,wake_id TEXT UNIQUE,metadata TEXT,content TEXT,
               PRIMARY KEY(owner,device_id,sequence));
            CREATE TABLE IF NOT EXISTS android_order_feed_receipts
              (wake_id TEXT,kind TEXT,principal TEXT,digest TEXT,PRIMARY KEY(wake_id,kind));
        ''')

    def _head(self, owner, device):
        self.events.db.execute('INSERT OR IGNORE INTO android_order_feed_heads VALUES(?,?,?,0)',
                               (owner, device, str(uuid.uuid4())))
        return dict(self.events.db.execute('SELECT * FROM android_order_feed_heads WHERE owner=? AND device_id=?',
                                           (owner, device)).fetchone())

    def _export(self, owner, device):
        """Caller owns the existing journal transaction. Freeze each original wake once, never retire it."""
        head = self._head(owner, device)
        wakes = self.events.db.execute('''SELECT w.* FROM android_order_wakes w LEFT JOIN android_order_feed f
            ON f.wake_id=w.wake_id WHERE w.owner=? AND w.device_id=? AND w.target='grok'
            AND w.kind IN ('question','status','handoff','observation') AND f.wake_id IS NULL
            AND w.state NOT IN ('done','cancelled','refused') ORDER BY w.rowid LIMIT 32''', (owner, device)).fetchall()
        for raw in wakes:
            wake = dict(raw)
            row = self.events._order(owner, wake['order_id'])
            try:
                self.events._authorize(wake['principal'], row)
            except OrderRefused:
                self.events.db.execute('UPDATE android_order_wakes SET hold_reason=? WHERE wake_id=?',
                                       ('access_changed', wake['wake_id']))
                continue
            payload = json.loads(wake['payload']) if wake['payload'] else self.events._payload(wake, row)
            if wake['payload'] is None:
                self.events.db.execute('UPDATE android_order_wakes SET payload=? WHERE wake_id=?',
                                       (document(payload), wake['wake_id']))
            origin = self.control.view(row, compact=True)['origin']
            ids = payload.get('question_ids', [q['id'] for q in payload.get('questions', []) if 'id' in q])
            metadata = dict(wake_id=wake['wake_id'], task_id=row.get('task_id') or row['original_request_id'],
                            request_id=wake['original_request_id'], order_id=row.get('provider_order_id') if row.get('task_id') is not None else row['order_id'],
                            owner_id=owner, device_id=device, source_id=row['source_id'], auth_epoch=row['auth_epoch'],
                            kind=wake['kind'], native_thread_id=wake['native_thread_id'],
                            native_turn_id=payload.get('native_turn_id', wake['native_turn_id']),
                            native_call_id=payload.get('native_call_id'), native_request_id=payload.get('native_request_id'),
                            item_id=payload.get('item_id'), message_id=payload.get('message_id'), question_ids=ids,
                            expires_at=datetime.fromtimestamp(self.events.clock()+self.lifetime_s, timezone.utc).isoformat())
            content = dict(obligation_ref=row['order_id'], origin=origin, payload=payload,
                           original_user_words=row['original_user_words'], metadata=metadata)
            head['sequence'] += 1
            self.events.db.execute('INSERT INTO android_order_feed VALUES(?,?,?,?,?,?)',
                                   (owner, device, head['sequence'], wake['wake_id'], document(metadata),
                                    json.dumps(content, sort_keys=True, separators=(',', ':'), allow_nan=False)))
        self.events.db.execute('UPDATE android_order_feed_heads SET sequence=? WHERE owner=? AND device_id=?',
                               (head['sequence'], owner, device))

    def _authorized(self, principal, owner, device, wake_id):
        wake = self.events.wake(wake_id)
        if wake['owner'] != owner or wake['device_id'] != device:
            raise OrderRefused('wake_not_owned')
        row = self.events._order(owner, wake['order_id'])
        self.events._authorize(principal, row)
        expected = row['recipient'] if row['responsible'] == 'grok' and wake['kind'] == 'observation' else wake['principal']
        if principal_id(principal) not in (owner, expected):
            raise OrderRefused('wake_not_owned')
        return wake, row

    def read_source(self, principal, owner, device_id, after, limit=4):
        if principal_id(principal) != owner or type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 16:
            raise OrderRefused('invalid_feed_cursor')
        with self.events.tx():
            self.events._authorize(principal, dict(owner=owner,device_id=device_id,order_id=None,task_id='feed',
                provider_order_id=None,source_id=self.control.source_id,auth_epoch=self.control.auth_epoch))
            self._export(owner, device_id)
            head = self._head(owner, device_id)
            rows = self.events.db.execute('''SELECT * FROM android_order_feed WHERE owner=? AND device_id=? AND sequence>?
                ORDER BY sequence LIMIT ?''', (owner, device_id, after, limit)).fetchall()
            items = []
            for row in rows:
                wake = self.events.wake(row['wake_id'])
                self.events._authorize(principal, self.events._order(owner, wake['order_id']))
                items.append(json.loads(row['metadata']) | {'sequence': row['sequence']})
            return dict(journal_id=head['journal_id'], after=after, next_after=items[-1]['sequence'] if items else after,
                        events=items, gap=after > head['sequence'])

    def fetch(self, principal, owner, device_id, wake_id):
        self._authorized(principal, owner, device_id, wake_id)
        row = self.events.db.execute('SELECT content FROM android_order_feed WHERE wake_id=? AND owner=? AND device_id=?',
                                     (wake_id, owner, device_id)).fetchone()
        if row is None:
            raise OrderRefused('wake_not_exported')
        return json.loads(row['content'])

    def ack_read(self, principal, owner, device_id, wake_id):
        """Authenticated Grok delivery/read receipt only; no native input or responsibility approval."""
        with self.events.tx():
            wake, row = self._authorized(principal, owner, device_id, wake_id)
            if wake['target'] != 'grok' or principal_id(principal) != wake['principal']:
                raise OrderRefused('receipt_not_owned')
            if not self.events.db.execute('SELECT 1 FROM android_order_feed WHERE wake_id=?', (wake_id,)).fetchone():
                raise OrderRefused('wake_not_exported')
            digest = hashlib.sha256(document([wake_id, 'read', principal_id(principal), owner, device_id]).encode()).hexdigest()
            old = self.events.db.execute("SELECT digest FROM android_order_feed_receipts WHERE wake_id=? AND kind='read'",
                                         (wake_id,)).fetchone()
            if old and old['digest'] != digest:
                raise OrderRefused('receipt_identity_conflict')
            if not old:
                if row['lifecycle'] != 'active' or wake['state'] not in ('pending', 'sending', 'unknown', 'accepted'):
                    raise OrderRefused('receipt_not_current')
                self.events.db.execute("INSERT INTO android_order_feed_receipts VALUES(?,'read',?,?)",
                                       (wake_id, principal_id(principal), digest))
                self.events.db.execute("UPDATE android_order_wakes SET state='accepted',hold_reason=NULL WHERE wake_id=?", (wake_id,))
            return dict(wake_id=wake_id, grok_state='read', native_status='not_observed')

    def ack_handoff(self, principal, owner, device_id, wake_id, *, snapshot_digest, next_check_at):
        wake, _ = self._authorized(principal, owner, device_id, wake_id)
        payload = json.loads(wake['payload'])
        if wake['kind'] != 'handoff' or payload['snapshot']['next_check_at'] != next_check_at:
            raise OrderRefused('handoff_snapshot_changed')
        if next_check_at is None or next_check_at <= self.events.clock():
            raise OrderRefused('handoff_next_check_stale')
        return self.events.ack_handoff(principal, wake_id, snapshot_digest=snapshot_digest)
