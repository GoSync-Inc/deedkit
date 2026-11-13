"""Private order obligations and observation wakes, without order business decisions.

The host authenticates each source/callback; authorize rechecks its current scope.
No phone, network client, scheduler process or LLM runs in this module.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import sqlite3
import time
import uuid


class OrderRefused(ValueError):
    """Stable content-free refusal; never include observation or credential text."""


def document(value):
    try:
        text = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError):
        raise OrderRefused('invalid_document') from None
    if len(text.encode()) > 16384:
        raise OrderRefused('document_too_large')
    return text


def timestamp(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise OrderRefused('invalid_timestamp')
    return value


@dataclass(frozen=True)
class Notification:
    event_id: str
    owner: str
    device_id: str
    order_id: str | None
    package: str
    source_id: str
    source_order_id: str | None
    auth_epoch: int
    observed_at: float
    observation_ref: str


class OrderEvents:
    def __init__(self, db, authorize, *, clock=time.time):
        self.db, self.authorize, self.clock = db, authorize, clock
        db.row_factory = sqlite3.Row
        db.executescript('''
            CREATE TABLE IF NOT EXISTS android_order_obligations (
              owner TEXT, order_id TEXT, device_id TEXT, package TEXT, source_id TEXT,
              source_order_id TEXT, auth_epoch INTEGER, original_request_id TEXT,
              native_thread_id TEXT, native_turn_id TEXT, question_id TEXT,
              original_user_words TEXT, next_check_at REAL, state_json TEXT,
              lifecycle TEXT DEFAULT 'active', responsible TEXT DEFAULT 'codex',
              recipient TEXT, revision INTEGER DEFAULT 1, seen INTEGER DEFAULT 0,
              handled INTEGER DEFAULT 0, PRIMARY KEY(owner,order_id));
            CREATE TABLE IF NOT EXISTS android_order_observations (
              owner TEXT, order_id TEXT, source_id TEXT, event_id TEXT, fingerprint TEXT,
              sequence INTEGER, observed_at REAL, observation_ref TEXT, correlation TEXT,
              PRIMARY KEY(owner,order_id,source_id,event_id));
            CREATE TABLE IF NOT EXISTS android_order_wakes (
              wake_id TEXT PRIMARY KEY, owner TEXT, order_id TEXT, device_id TEXT,
              kind TEXT, target TEXT, principal TEXT, through INTEGER, revision INTEGER,
              state TEXT DEFAULT 'pending', hold_reason TEXT, payload TEXT,
              original_request_id TEXT, native_thread_id TEXT, native_turn_id TEXT, question_id TEXT);
            CREATE UNIQUE INDEX IF NOT EXISTS android_order_one_monitor ON android_order_wakes(owner,order_id)
              WHERE kind='observation' AND state IN ('pending','sending','unknown','accepted');
            CREATE UNIQUE INDEX IF NOT EXISTS android_order_one_handoff ON android_order_wakes(owner,order_id)
              WHERE kind='handoff' AND state IN ('pending','sending','unknown','accepted');
            CREATE UNIQUE INDEX IF NOT EXISTS android_order_one_delivery ON android_order_wakes(device_id)
              WHERE state='sending';
        ''')

    @contextmanager
    def tx(self):
        # One connection per host service; independent connections serialize at admission.
        if self.db.in_transaction:
            raise OrderRefused('transaction_already_open')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def _order(self, owner, order_id):
        row = self.db.execute('SELECT * FROM android_order_obligations WHERE owner=? AND order_id=?',
                              (owner, order_id)).fetchone()
        if not row:
            raise OrderRefused('order_not_bound')
        return dict(row)

    def _authorize(self, principal, row):
        try:
            allowed = self.authorize(principal, row['owner'], row['device_id'], row['order_id'],
                                     row['source_id'], row['auth_epoch']) is True
        except Exception:
            allowed = False
        if not allowed:
            raise OrderRefused('access_changed')

    def order(self, principal, owner, order_id):
        row = self._order(owner, order_id)
        self._authorize(principal, row)
        holds = self.db.execute('''SELECT wake_id,kind,state,hold_reason FROM android_order_wakes
            WHERE owner=? AND order_id=? AND state IN ('pending','sending','unknown','accepted','refused')''',
            (owner, order_id)).fetchall()
        return row | {'state': json.loads(row['state_json']), 'delivery': [dict(r) for r in holds]}

    def orders(self, principal, owner):
        rows = self.db.execute('SELECT order_id FROM android_order_obligations WHERE owner=?', (owner,)).fetchall()
        return [self.order(principal, owner, row['order_id']) for row in rows]

    def bind(self, principal, *, owner, device_id, order_id, package, source_id, source_order_id,
             auth_epoch, original_request_id, native_thread_id, original_user_words, next_check_at,
             native_turn_id=None, question_id=None, state=None):
        if principal != owner:
            raise OrderRefused('binding_not_owned')
        values = locals()
        for name in ('owner', 'device_id', 'order_id', 'package', 'source_id', 'source_order_id',
                     'original_request_id', 'native_thread_id', 'original_user_words'):
            if not isinstance(values[name], str) or not 1 <= len(values[name]) <= 16384:
                raise OrderRefused('invalid_binding')
        if type(auth_epoch) is not int or auth_epoch < 1:
            raise OrderRefused('invalid_binding')
        timestamp(next_check_at)
        row = {name: values[name] for name in ('owner', 'device_id', 'order_id', 'source_id', 'auth_epoch')}
        self._authorize(principal, row)
        with self.tx():
            if self.db.execute('SELECT 1 FROM android_order_obligations WHERE owner=? AND order_id=?',
                               (owner, order_id)).fetchone():
                raise OrderRefused('order_already_bound')
            self.db.execute('''INSERT INTO android_order_obligations
                (owner,order_id,device_id,package,source_id,source_order_id,auth_epoch,original_request_id,
                 native_thread_id,native_turn_id,question_id,original_user_words,next_check_at,state_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (owner, order_id, device_id, package, source_id, source_order_id, auth_epoch, original_request_id,
                 native_thread_id, native_turn_id, question_id, original_user_words, next_check_at, document(state or {})))

    def _stage(self, row):
        active = self.db.execute('''SELECT * FROM android_order_wakes WHERE owner=? AND order_id=?
            AND kind='observation' AND state IN ('pending','sending','unknown','accepted')''',
            (row['owner'], row['order_id'])).fetchone()
        if active:
            if active['state'] == 'pending':
                self.db.execute('UPDATE android_order_wakes SET through=? WHERE wake_id=?',
                                (row['seen'], active['wake_id']))
            return active['wake_id']
        ident = str(uuid.uuid4())  # delivery identity only; never a replacement native request ID
        self.db.execute('''INSERT INTO android_order_wakes
            (wake_id,owner,order_id,device_id,kind,target,principal,through,revision,
             original_request_id,native_thread_id,native_turn_id,question_id)
            VALUES(?,?,?,?,'observation',?,?,?,?,?,?,?,?)''',
            (ident, row['owner'], row['order_id'], row['device_id'], row['responsible'],
             row['recipient'] if row['responsible'] == 'grok' else row['owner'], row['seen'], row['revision'],
             row['original_request_id'], row['native_thread_id'], row['native_turn_id'], row['question_id']))
        return ident

    def observe(self, principal, observation):
        data = asdict(observation)
        if type(observation.auth_epoch) is not int or observation.auth_epoch < 1:
            raise OrderRefused('invalid_observation')
        for key in ('order_id', 'source_order_id'):
            if data[key] is not None and (not isinstance(data[key], str) or not 1 <= len(data[key]) <= 256):
                raise OrderRefused('invalid_observation')
        for key in ('event_id', 'owner', 'device_id', 'package', 'source_id', 'observation_ref'):
            if not isinstance(data[key], str) or not 1 <= len(data[key]) <= 256:
                raise OrderRefused('invalid_observation')
        timestamp(observation.observed_at)
        if not self.clock() - 300 <= observation.observed_at <= self.clock() + 60:
            raise OrderRefused('stale_observation')
        fingerprint = hashlib.sha256(document(data).encode()).hexdigest()
        with self.tx():
            if observation.order_id is None:
                if observation.source_order_id is not None:
                    raise OrderRefused('ambiguous_correlation')
                rows = [dict(r) for r in self.db.execute('''SELECT * FROM android_order_obligations
                    WHERE owner=? AND device_id=? AND package=? AND source_id=? AND lifecycle='active' ''',
                    (observation.owner, observation.device_id, observation.package, observation.source_id))]
                if not rows:
                    raise OrderRefused('order_not_bound')
            else:
                rows = [self._order(observation.owner, observation.order_id)]
            recorded = False
            for row in rows:
                self._authorize(principal, row)
                if row['lifecycle'] != 'active':
                    raise OrderRefused('order_closed')
                if any(data[k] != row[k] for k in ('device_id', 'package', 'source_id', 'auth_epoch')):
                    raise OrderRefused('observation_binding_mismatch')
                if observation.order_id is not None and observation.source_order_id != row['source_order_id']:
                    raise OrderRefused('observation_binding_mismatch')
                prior = self.db.execute('''SELECT fingerprint FROM android_order_observations
                    WHERE owner=? AND order_id=? AND source_id=? AND event_id=?''',
                    (row['owner'], row['order_id'], observation.source_id, observation.event_id)).fetchone()
                if prior:
                    if prior['fingerprint'] != fingerprint:
                        raise OrderRefused('event_identity_conflict')
                    continue
                row['seen'] += 1
                self.db.execute('''INSERT INTO android_order_observations VALUES(?,?,?,?,?,?,?,?,?)''',
                    (row['owner'], row['order_id'], observation.source_id, observation.event_id, fingerprint,
                     row['seen'], observation.observed_at, observation.observation_ref,
                     'exact' if observation.order_id else 'unresolved_app'))
                self.db.execute('UPDATE android_order_obligations SET seen=? WHERE owner=? AND order_id=?',
                                (row['seen'], row['owner'], row['order_id']))
                self._stage(row)
                recorded = True
        return 'recorded' if recorded else 'duplicate'

    def observations(self, principal, owner, order_id):
        self.order(principal, owner, order_id)
        return [dict(r) for r in self.db.execute('''SELECT event_id,sequence,observed_at,observation_ref,correlation
            FROM android_order_observations WHERE owner=? AND order_id=? ORDER BY sequence''', (owner, order_id))]

    def stage_due(self):
        with self.tx():
            for row in self.db.execute('''SELECT * FROM android_order_obligations
                WHERE lifecycle='active' AND next_check_at<=?''', (self.clock(),)).fetchall():
                self._stage(dict(row))

    def wake(self, wake_id):
        row = self.db.execute('SELECT * FROM android_order_wakes WHERE wake_id=?', (wake_id,)).fetchone()
        if not row:
            raise OrderRefused('wake_not_found')
        return dict(row)

    def pending(self):
        """Internal host work queue; API callers use authorized order/observations reads."""
        return [dict(r) for r in self.db.execute("SELECT * FROM android_order_wakes WHERE state='pending'")]

    def hold(self, wake_id, reason):
        with self.tx():
            self.db.execute("UPDATE android_order_wakes SET hold_reason=? WHERE wake_id=? AND state='pending'",
                            (reason, wake_id))

    def claim(self, wake_id):
        with self.tx():
            wake = self.wake(wake_id)
            if wake['state'] != 'pending':
                return False
            row = self._order(wake['owner'], wake['order_id'])
            try:
                self._authorize(wake['principal'], row)
            except OrderRefused:
                self.db.execute('UPDATE android_order_wakes SET hold_reason=? WHERE wake_id=?',
                                ('access_changed', wake_id))
                return False
            if row['lifecycle'] != 'active':
                self.db.execute("UPDATE android_order_wakes SET state='cancelled' WHERE wake_id=?", (wake_id,))
                return False
            if self.db.execute("SELECT 1 FROM android_order_wakes WHERE device_id=? AND state='sending'",
                               (row['device_id'],)).fetchone():
                return False
            # Freeze exactly the admitted snapshot before any await/downstream write.
            payload = self._payload(wake, row)
            self.db.execute("UPDATE android_order_wakes SET state='sending',hold_reason=NULL,payload=? WHERE wake_id=?",
                            (document(payload), wake_id))
            return True

    def _payload(self, wake, row):
        if wake['kind'] != 'observation':
            return json.loads(wake['payload'])
        return {'wake_id': wake['wake_id'], 'order_id': row['order_id'], 'source': row['source_id'],
                'package': row['package'], 'through': wake['through'], 'observations_untrusted': True,
                'next_check_at': row['next_check_at'], 'state': json.loads(row['state_json']),
                'observations': self.observations(wake['principal'], row['owner'], row['order_id'])[-100:]}

    def envelope(self, wake_id):
        wake = self.wake(wake_id)
        row = self._order(wake['owner'], wake['order_id'])
        payload = json.loads(wake['payload']) if wake['payload'] else self._payload(wake, row)
        return {'owner': wake['owner'], 'device_id': wake['device_id'],
                'original_request_id': wake['original_request_id'], 'native_thread_id': wake['native_thread_id'],
                'native_turn_id': wake['native_turn_id'], 'question_id': wake['question_id'],
                'native_request_id': payload.get('native_request_id'), 'item_id': payload.get('item_id'),
                'kind': wake['kind'], 'payload': payload}

    def _complete(self, wake_id, receipt):
        wake = self.wake(wake_id)
        if wake['state'] not in ('sending', 'unknown'):
            raise OrderRefused('wake_not_reconcilable')
        for field in ('original_request_id', 'native_thread_id', 'native_turn_id', 'question_id'):
            if receipt.get(field) != wake[field]:
                raise OrderRefused('receipt_identity_mismatch')
        payload = json.loads(wake['payload']) if wake['payload'] else {}
        for field in ('native_request_id', 'item_id'):
            if field in payload and (type(receipt.get(field)) is not type(payload[field])
                                     or receipt.get(field) != payload[field]):
                raise OrderRefused('receipt_identity_mismatch')
        state = receipt.get('status')
        if state not in ('accepted', 'unknown', 'refused'):
            raise OrderRefused('invalid_receipt')
        # Only busy is a documented pre-delivery refusal safe to offer at the next boundary.
        reason = receipt.get('reason')
        state = 'pending' if state == 'refused' and reason == 'device_busy' else state
        self.db.execute('UPDATE android_order_wakes SET state=?,hold_reason=? WHERE wake_id=?',
                        (state, reason if reason in ('device_busy', 'access_changed', 'receiver_unavailable') else None,
                         wake_id))

    def complete(self, wake_id, receipt):
        with self.tx():
            self._complete(wake_id, receipt)

    def reconcile(self, principal, wake_id, receipt):
        with self.tx():
            wake = self.wake(wake_id)
            self._authorize(principal, self._order(wake['owner'], wake['order_id']))
            if principal != wake['principal']:
                raise OrderRefused('receipt_not_owned')
            if wake['state'] != 'unknown' or receipt.get('status') != 'accepted':
                raise OrderRefused('receipt_not_confirmation')
            self._complete(wake_id, receipt)

    def unknown(self, wake_id):
        with self.tx():
            self.db.execute("UPDATE android_order_wakes SET state='unknown',hold_reason='delivery_unknown' "
                            "WHERE wake_id=? AND state='sending'", (wake_id,))

    def recover(self):
        """Service boot ONLY, under the existing Store process lock; never on an active peer."""
        with self.tx():
            self.db.execute("UPDATE android_order_wakes SET state='unknown',hold_reason='delivery_unknown' "
                            "WHERE state='sending'")

    def checked(self, principal, wake_id, *, next_check_at, state, lifecycle='active'):
        if lifecycle not in ('active', 'received', 'cancelled'):
            raise OrderRefused('invalid_lifecycle')
        if lifecycle == 'active':
            timestamp(next_check_at)
            if next_check_at <= self.clock():
                raise OrderRefused('next_check_not_future')
        elif next_check_at is not None:
            raise OrderRefused('closed_order_has_timer')
        state_json = document(state)
        with self.tx():
            wake = self.wake(wake_id)
            row = self._order(wake['owner'], wake['order_id'])
            self._authorize(principal, row)
            expected = row['recipient'] if row['responsible'] == 'grok' else row['owner']
            if principal != expected or wake['state'] != 'accepted' or wake['kind'] != 'observation':
                raise OrderRefused('check_not_owned')
            self.db.execute("UPDATE android_order_wakes SET state='done' WHERE wake_id=?", (wake_id,))
            self.db.execute('''UPDATE android_order_obligations SET handled=?,state_json=?,next_check_at=?,
                lifecycle=?,revision=revision+1 WHERE owner=? AND order_id=?''',
                (wake['through'], state_json, next_check_at, lifecycle, row['owner'], row['order_id']))
            if lifecycle != 'active':
                self.db.execute("UPDATE android_order_wakes SET state='cancelled' WHERE owner=? AND order_id=? "
                                "AND state='pending'", (row['owner'], row['order_id']))
            elif row['seen'] > wake['through']:
                self._stage(self._order(row['owner'], row['order_id']))

    def _snapshot(self, row):
        return {key: row[key] for key in ('owner', 'order_id', 'device_id', 'source_id', 'package',
                'original_request_id', 'native_thread_id', 'native_turn_id', 'question_id', 'original_user_words',
                'next_check_at', 'state_json', 'revision', 'seen', 'handled')}

    def propose_handoff(self, principal, owner, order_id, *, target_principal):
        if not isinstance(target_principal, str) or not 1 <= len(target_principal) <= 128:
            raise OrderRefused('invalid_recipient')
        with self.tx():
            row = self._order(owner, order_id)
            self._authorize(principal, row)
            if principal != owner or row['responsible'] != 'codex' or row['lifecycle'] != 'active':
                raise OrderRefused('handoff_not_owned')
            if self.db.execute("SELECT 1 FROM android_order_wakes WHERE owner=? AND order_id=? "
                               "AND state IN ('sending','unknown','accepted')", (owner, order_id)).fetchone():
                raise OrderRefused('order_delivery_unsettled')
            snapshot = self._snapshot(row)
            payload = {'snapshot': snapshot, 'snapshot_digest': hashlib.sha256(document(snapshot).encode()).hexdigest(),
                       'responsibility_proposal': True, 'source': 'codex'}
            ident = str(uuid.uuid4())
            try:
                self.db.execute('''INSERT INTO android_order_wakes
                    (wake_id,owner,order_id,device_id,kind,target,principal,through,revision,payload,
                     original_request_id,native_thread_id,native_turn_id,question_id)
                    VALUES(?,?,?,?,'handoff','grok',?,?,?,?,?,?,?,?)''',
                    (ident, owner, order_id, row['device_id'], target_principal, row['seen'], row['revision'],
                     document(payload), row['original_request_id'], row['native_thread_id'], row['native_turn_id'],
                     row['question_id']))
            except sqlite3.IntegrityError:
                raise OrderRefused('handoff_already_pending') from None
            return ident

    def ack_handoff(self, principal, wake_id, *, snapshot_digest):
        with self.tx():
            wake = self.wake(wake_id)
            row = self._order(wake['owner'], wake['order_id'])
            self._authorize(principal, row)
            if principal != wake['principal'] or wake['kind'] != 'handoff':
                raise OrderRefused('handoff_not_delivered')
            payload = json.loads(wake['payload'])
            if (wake['state'] == 'done' and row['responsible'] == 'grok' and row['recipient'] == principal
                    and snapshot_digest == payload['snapshot_digest']):
                return {'responsible': 'grok', 'acknowledged': True}
            if wake['state'] != 'accepted':
                raise OrderRefused('handoff_not_delivered')
            if snapshot_digest != payload['snapshot_digest'] or self._snapshot(row) != payload['snapshot']:
                raise OrderRefused('handoff_snapshot_changed')
            if self.db.execute("SELECT 1 FROM android_order_wakes WHERE owner=? AND order_id=? AND kind='observation' "
                               "AND state IN ('sending','unknown','accepted')", (row['owner'], row['order_id'])).fetchone():
                raise OrderRefused('order_delivery_unsettled')
            self.db.execute("UPDATE android_order_obligations SET responsible='grok',recipient=? "
                            "WHERE owner=? AND order_id=?", (principal, row['owner'], row['order_id']))
            self.db.execute("UPDATE android_order_wakes SET state='done' WHERE wake_id=?", (wake_id,))
            self.db.execute("UPDATE android_order_wakes SET target='grok',principal=? "
                            "WHERE owner=? AND order_id=? AND state='pending' AND kind='observation'",
                            (principal, row['owner'], row['order_id']))
            return {'responsible': 'grok', 'acknowledged': True}

    def withdraw_handoff(self, principal, wake_id):
        """Supersede a known unswitched proposal. An unknown send still requires reconciliation."""
        with self.tx():
            wake = self.wake(wake_id)
            row = self._order(wake['owner'], wake['order_id'])
            self._authorize(principal, row)
            if (principal != row['owner'] or row['responsible'] != 'codex' or wake['kind'] != 'handoff'
                    or wake['state'] not in ('pending', 'accepted', 'refused')):
                raise OrderRefused('handoff_not_withdrawable')
            self.db.execute("UPDATE android_order_wakes SET state='cancelled' WHERE wake_id=?", (wake_id,))

    def notify(self, principal, owner, order_id, *, notice_id, kind, native_turn_id,
               question_id=None, native_request_id=None, item_id=None, payload):
        """Core-originated nonsecret question/status to caller; never called by a push source."""
        if kind not in ('question', 'status') or principal != owner:
            raise OrderRefused('notice_not_owned')
        if not isinstance(notice_id, str) or not 1 <= len(notice_id) <= 256:
            raise OrderRefused('invalid_notice_id')
        if kind == 'question':
            if (type(native_request_id) not in (str, int) or not item_id or not question_id
                    or not native_turn_id or payload.get('method') != 'item/tool/requestUserInput'):
                raise OrderRefused('not_user_input_question')
            questions = payload.get('questions')
            if not isinstance(questions, list) or not 1 <= len(questions) <= 32:
                raise OrderRefused('invalid_questions')
            ids = [q.get('id') for q in questions if isinstance(q, dict)]
            if (len(ids) != len(questions) or any(not isinstance(q, str) or not q for q in ids)
                    or len(set(ids)) != len(ids) or question_id not in ids):
                raise OrderRefused('invalid_questions')
        # Native request IDs retain JSON type: 42 and "42" are different requests.
        content = document(payload | {'native_request_id': native_request_id, 'item_id': item_id,
                                      'question_id': question_id, 'source': 'native'})
        with self.tx():
            row = self._order(owner, order_id)
            self._authorize(principal, row)
            identity = ([owner, order_id, kind, row['native_thread_id'], native_turn_id,
                         native_request_id, item_id, question_id] if kind == 'question'
                        else [owner, order_id, notice_id])
            ident = str(uuid.uuid5(uuid.NAMESPACE_URL, document(identity)))
            if row['lifecycle'] != 'active':
                raise OrderRefused('order_closed')
            previous = self.db.execute('SELECT * FROM android_order_wakes WHERE wake_id=?', (ident,)).fetchone()
            if previous:
                if (previous['payload'] != content or previous['kind'] != kind
                        or previous['native_turn_id'] != native_turn_id):
                    raise OrderRefused('notice_identity_conflict')
                return ident
            self.db.execute('''INSERT INTO android_order_wakes
                (wake_id,owner,order_id,device_id,kind,target,principal,through,revision,payload,
                 original_request_id,native_thread_id,native_turn_id,question_id)
                VALUES(?,?,?,?,?,'grok',?,?,?,?,?,?,?,?)''',
                (ident, owner, order_id, row['device_id'], kind, owner, row['seen'], row['revision'], content,
                 row['original_request_id'], row['native_thread_id'], native_turn_id, question_id))
            return ident

    def answer(self, principal, question_wake_id, *, native_request_id, item_id, answers):
        """Prepare one correlated natural answer; A validates the live native RPC and safe boundary."""
        with self.tx():
            wake = self.wake(question_wake_id)
            row = self._order(wake['owner'], wake['order_id'])
            self._authorize(principal, row)
            if principal != row['owner'] or wake['kind'] != 'question' or wake['state'] != 'accepted':
                raise OrderRefused('question_not_delivered')
            question = json.loads(wake['payload'])
            if (type(native_request_id) is not type(question['native_request_id'])
                    or native_request_id != question['native_request_id'] or item_id != question['item_id']):
                raise OrderRefused('answer_identity_mismatch')
            if (not isinstance(answers, dict) or set(answers) != {q['id'] for q in question['questions']}
                    or any(not isinstance(values, list) or not values or
                           any(not isinstance(v, str) or not v for v in values) for values in answers.values())):
                raise OrderRefused('invalid_answers')
            content = document({'native_request_id': native_request_id, 'item_id': item_id,
                                'question_id': wake['question_id'], 'answers': answers, 'source': 'caller'})
            ident = str(uuid.uuid5(uuid.NAMESPACE_URL, question_wake_id + ':answer'))
            previous = self.db.execute('SELECT payload FROM android_order_wakes WHERE wake_id=?', (ident,)).fetchone()
            if previous:
                if previous['payload'] != content:
                    raise OrderRefused('answer_identity_conflict')
                return ident
            self.db.execute('''INSERT INTO android_order_wakes
                (wake_id,owner,order_id,device_id,kind,target,principal,through,revision,payload,
                 original_request_id,native_thread_id,native_turn_id,question_id)
                VALUES(?,?,?,?,'answer','codex',?,?,?,?,?,?,?,?)''',
                (ident, wake['owner'], wake['order_id'], wake['device_id'], principal, wake['through'],
                 wake['revision'], content, wake['original_request_id'], wake['native_thread_id'],
                 wake['native_turn_id'], wake['question_id']))
            return ident
