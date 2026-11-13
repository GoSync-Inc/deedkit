"""Native-only metadata control over the existing obligation journal; no device/RPC executor."""
from dataclasses import dataclass, fields
import hashlib
import json
import uuid

from .order_events import OrderRefused, document, timestamp


@dataclass(frozen=True)
class NativeOrderContext:
    principal: object
    owner: str
    device_id: str
    task_id: str
    request_id: str
    native_thread_id: str
    native_turn_id: str
    native_call_id: str | None
    native_request_id: str | int | None
    item_id: str | None = None
    native_namespace: str | None = None


def ident(value, *, optional=False):
    if optional and value is None:
        return value
    if not isinstance(value, str) or not 1 <= len(value) <= 256 or '\0' in value:
        raise OrderRefused('invalid_control_identity')
    return value


def actor(context):
    # The Principal contains an authentication hash: never serialize it.
    return {field.name: getattr(context, field.name) for field in fields(context) if field.name != 'principal'}


def principal_id(principal):
    return getattr(principal, 'id', principal)


class OrderControl:
    OPERATIONS = frozenset(('bind', 'update', 'next_check', 'notify', 'handoff'))
    FIELDS = {
        'bind': {'package', 'provider_order_id', 'source_order_id', 'next_check_at', 'state'},
        'update': {'obligation_ref', 'changes'},
        'next_check': {'obligation_ref', 'at', 'checked_wake_id'},
        'notify': {'obligation_ref', 'message'},
        'handoff': {'obligation_ref', 'target_principal', 'withdraw_wake_id'},
    }
    CHANGES = frozenset(('package', 'provider_order_id', 'source_order_id', 'state', 'lifecycle'))

    def __init__(self, events, *, source_id, auth_epoch, original_words):
        self.events, self.source_id, self.auth_epoch, self.original_words = events, ident(source_id), auth_epoch, original_words
        if type(auth_epoch) is not int or auth_epoch < 1 or not callable(original_words):
            raise OrderRefused('invalid_control_source')
        columns = {r[1] for r in events.db.execute('PRAGMA table_info(android_order_obligations)')}
        for name, declaration in (('task_id', 'TEXT'), ('provider_order_id', 'TEXT'),
                                  ('origin_json', 'TEXT'), ('task_only', 'INTEGER DEFAULT 0')):
            if name not in columns:
                events.db.execute(f'ALTER TABLE android_order_obligations ADD COLUMN {name} {declaration}')
        events.db.execute('''CREATE TABLE IF NOT EXISTS android_order_controls
            (call_key TEXT PRIMARY KEY,digest TEXT,response TEXT)''')
        events.db.commit()
        from .order_events_feed import OrderFeed
        self.feed = OrderFeed(self)

    def _context(self, ctx, *, call=True):
        if not isinstance(ctx, NativeOrderContext) or principal_id(ctx.principal) != ctx.owner:
            raise OrderRefused('control_not_owned')
        for name in ('owner', 'device_id', 'task_id', 'request_id', 'native_thread_id', 'native_turn_id'):
            ident(getattr(ctx, name))
        for name in ('native_call_id', 'item_id', 'native_namespace'):
            ident(getattr(ctx, name), optional=True)
        if type(ctx.native_request_id) not in (str, int, type(None)):
            raise OrderRefused('invalid_control_identity')
        if call and (ctx.native_call_id is None or ctx.native_request_id is None):
            raise OrderRefused('native_control_call_required')

    def _base(self, ctx, provider=None):
        return dict(owner=ctx.owner, device_id=ctx.device_id, order_id=None, task_id=ctx.task_id,
                    provider_order_id=provider, source_id=self.source_id, auth_epoch=self.auth_epoch)

    def _target(self, ctx, ref):
        row = self.events._order(ctx.owner, ident(ref))
        if row['device_id'] != ctx.device_id or row['native_thread_id'] != ctx.native_thread_id:
            raise OrderRefused('control_target_mismatch')
        self.events._authorize(ctx.principal, row)
        return row

    def _words(self, ctx):
        try:
            words = self.original_words(ctx)
        except Exception:
            raise OrderRefused('original_task_unavailable') from None
        if not isinstance(words, str) or not 1 <= len(words) <= 16384:
            raise OrderRefused('original_task_unavailable')
        return words

    def _insert(self, ctx, ref, *, notice_only, next_check_at=None, state=None,
                package=None, provider_order_id=None, source_order_id=None):
        self.events._authorize(ctx.principal, self._base(ctx, provider_order_id))
        self.events.db.execute('''INSERT INTO android_order_obligations
            (owner,order_id,device_id,package,source_id,source_order_id,auth_epoch,original_request_id,
             native_thread_id,native_turn_id,original_user_words,next_check_at,state_json,task_id,
             provider_order_id,origin_json,task_only) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (ctx.owner, ref, ctx.device_id, package, self.source_id, source_order_id, self.auth_epoch,
             ctx.request_id, ctx.native_thread_id, ctx.native_turn_id, self._words(ctx), next_check_at,
             document({} if state is None else state), ctx.task_id, provider_order_id, document(actor(ctx)), int(notice_only)))
        return self.events._order(ctx.owner, ref)

    def _task_ref(self, ctx):
        identity = document([ctx.owner, ctx.device_id, ctx.native_thread_id, ctx.task_id, ctx.request_id])
        return 'obligation:' + str(uuid.uuid5(uuid.NAMESPACE_URL, 'task-notice:' + identity))

    def _notice_row(self, ctx):
        ref = self._task_ref(ctx)
        exists = self.events.db.execute('SELECT 1 FROM android_order_obligations WHERE owner=? AND order_id=?',
                                        (ctx.owner, ref)).fetchone()
        return self._target(ctx, ref) if exists else self._insert(ctx, ref, notice_only=True)

    def view(self, row, *, compact=False):
        origin = json.loads(row['origin_json']) if row.get('origin_json') else {
            'task_id': row['original_request_id'], 'request_id': row['original_request_id'],
            'native_thread_id': row['native_thread_id'], 'native_turn_id': row['native_turn_id']}
        holds = [dict(r) for r in self.events.db.execute('''SELECT wake_id,kind,state,hold_reason
            FROM android_order_wakes WHERE owner=? AND order_id=? AND state NOT IN ('done','cancelled')
            ORDER BY CASE state WHEN 'unknown' THEN 0 WHEN 'sending' THEN 1 WHEN 'pending' THEN 2 ELSE 3 END,rowid LIMIT 17''',
            (row['owner'], row['order_id']))]
        result = dict(obligation_ref=row['order_id'], provider_order_id=row.get('provider_order_id') if row.get('task_id') is not None else row['order_id'],
                      package=row['package'], source_order_id=row['source_order_id'], origin=origin,
                      lifecycle=row['lifecycle'], responsible=row['responsible'], next_check_at=row['next_check_at'],
                      notice_only=bool(row.get('task_only')), revision=row['revision'], delivery=holds[:16],
                      delivery_more=len(holds)>16)
        if not compact:
            result.update(state=json.loads(row['state_json']), original_user_words=row['original_user_words'])
        return result

    def _provider_changes(self, row, changes):
        for key in ('package', 'provider_order_id', 'source_order_id'):
            if key in changes:
                ident(changes[key], optional=True)
                if row.get(key) is not None and changes[key] != row[key]:
                    raise OrderRefused('provider_binding_conflict')

    def _bind(self, ctx, payload, key):
        at = timestamp(payload.get('next_check_at'))
        if at <= self.events.clock():
            raise OrderRefused('next_check_not_future')
        self._provider_changes({}, payload)
        ref = self._task_ref(ctx)
        old = self.events.db.execute('SELECT * FROM android_order_obligations WHERE owner=? AND order_id=?',
                                     (ctx.owner, ref)).fetchone()
        if old and old['task_only']:
            row = self._target(ctx, ref)
            changes = {k: v for k, v in payload.items() if k != 'next_check_at'}
            self._change(ctx, row, changes)
            self.events.db.execute('UPDATE android_order_obligations SET task_only=0,next_check_at=? WHERE owner=? AND order_id=?',
                                   (at, ctx.owner, ref))
        else:
            ref = 'obligation:' + str(uuid.uuid5(uuid.NAMESPACE_URL, key))
            self._insert(ctx, ref, notice_only=False, next_check_at=at, **{k: v for k, v in payload.items() if k != 'next_check_at'})
        row = self.events._order(ctx.owner, ref)
        self._unique_provider(row)
        return row

    def _unique_provider(self, row):
        provider = row['provider_order_id']
        if provider is not None and self.events.db.execute('''SELECT 1 FROM android_order_obligations WHERE owner=?
                AND device_id=? AND package IS ? AND provider_order_id=? AND order_id<>? AND lifecycle='active' ''',
                (row['owner'], row['device_id'], row['package'], provider, row['order_id'])).fetchone():
            raise OrderRefused('provider_already_bound')

    def _change(self, ctx, row, changes):
        if not isinstance(changes, dict) or not changes.keys() <= self.CHANGES:
            raise OrderRefused('invalid_control_changes')
        if not changes:
            return
        if row['lifecycle'] != 'active' or row['responsible'] != 'codex':
            raise OrderRefused('responsibility_not_owned')
        self._provider_changes(row, changes)
        candidate = row | changes
        self._unique_provider(candidate)
        self.events._authorize(ctx.principal, candidate)
        lifecycle = candidate['lifecycle']
        if lifecycle not in ('active', 'received', 'cancelled'):
            raise OrderRefused('invalid_lifecycle')
        for key, value in changes.items():
            column = 'state_json' if key == 'state' else key
            self.events.db.execute(f'UPDATE android_order_obligations SET {column}=? WHERE owner=? AND order_id=?',
                                   (document(value) if key == 'state' else value, row['owner'], row['order_id']))
        self.events.db.execute('UPDATE android_order_obligations SET revision=revision+1 WHERE owner=? AND order_id=?',
                               (row['owner'], row['order_id']))
        if lifecycle != 'active':
            self.events.db.execute('UPDATE android_order_obligations SET next_check_at=NULL WHERE owner=? AND order_id=?',
                                   (row['owner'], row['order_id']))
            self.events.db.execute("UPDATE android_order_wakes SET state='cancelled' WHERE owner=? AND order_id=? AND state='pending'",
                                   (row['owner'], row['order_id']))

    async def __call__(self, ctx, operation, payload):
        self._context(ctx)
        if operation not in self.OPERATIONS or not isinstance(payload, dict) or not payload.keys() <= self.FIELDS[operation]:
            raise OrderRefused('unsupported_order_control')
        body = document([actor(ctx), operation, payload])
        key = hashlib.sha256(document([ctx.owner, ctx.device_id, ctx.native_thread_id, ctx.native_namespace,
                                       ctx.native_turn_id, ctx.native_call_id]).encode()).hexdigest()
        digest = hashlib.sha256(body.encode()).hexdigest()
        with self.events.tx():
            ref = payload.get('obligation_ref')
            row = self._target(ctx, ref) if ref else None
            if row is None:
                self.events._authorize(ctx.principal, self._base(ctx))
            previous = self.events.db.execute('SELECT * FROM android_order_controls WHERE call_key=?', (key,)).fetchone()
            if previous:
                if previous['digest'] != digest:
                    raise OrderRefused('control_identity_conflict')
                result = json.loads(previous['response'])
                references = [result['obligation_ref']] if result.get('obligation_ref') else []
                references.extend(item['obligation_ref'] for item in result.get('items', []))
                for reference in references:
                    self._target(ctx, reference)  # Cached reads never reauthorize an older source epoch.
                return result
            result = dict(status='applied', effect='metadata_only', actor=actor(ctx))
            if operation == 'bind':
                row = self._bind(ctx, payload, key)
            elif operation == 'update':
                changes = payload.get('changes', {})
                if row is None:
                    if changes:
                        raise OrderRefused('obligation_ref_required')
                    rows = [dict(r) for r in self.events.db.execute('''SELECT * FROM android_order_obligations
                        WHERE owner=? AND device_id=? AND native_thread_id=? AND lifecycle='active' ORDER BY order_id LIMIT 9''',
                        (ctx.owner, ctx.device_id, ctx.native_thread_id))]
                    for item in rows:
                        self.events._authorize(ctx.principal, item)
                    result.update(items=[self.view(r, compact=True) for r in rows[:8]], more=len(rows)>8)
                else:
                    self._change(ctx, row, changes)
                    result['snapshot'] = self.view(self.events._order(ctx.owner, ref))
            elif operation == 'next_check':
                if row is None:
                    raise OrderRefused('obligation_ref_required')
                at = timestamp(payload.get('at'))
                if at <= self.events.clock() or row['lifecycle'] != 'active' or row['responsible'] != 'codex':
                    raise OrderRefused('next_check_not_owned')
                if 'checked_wake_id' in payload:
                    wake = self.events.wake(ident(payload['checked_wake_id']))
                    if wake['owner'] != ctx.owner or wake['order_id'] != ref:
                        raise OrderRefused('check_not_owned')
                    self.events._checked(ctx.principal, wake['wake_id'], next_check_at=at,
                                         state=json.loads(row['state_json']))
                    result['checked_wake_id'] = wake['wake_id']
                else:
                    self.events.db.execute('''UPDATE android_order_obligations SET next_check_at=?,revision=revision+1
                        WHERE owner=? AND order_id=?''', (at, ctx.owner, ref))
            elif operation == 'notify':
                row = row or self._notice_row(ctx)
                message = payload.get('message')
                if not isinstance(message, str) or not 1 <= len(message) <= 8192:
                    raise OrderRefused('invalid_notice')
                result['wake_id'] = self._notice(ctx, row, 'status', {'message': message}, key)
            elif operation == 'handoff':
                if row is None:
                    raise OrderRefused('obligation_ref_required')
                if 'withdraw_wake_id' in payload:
                    if 'target_principal' in payload:
                        raise OrderRefused('invalid_handoff')
                    prior = self.events.wake(ident(payload['withdraw_wake_id']))
                    if prior['owner'] != ctx.owner or prior['order_id'] != ref:
                        raise OrderRefused('handoff_not_owned')
                    self.events._withdraw_handoff(ctx.principal, prior['wake_id'])
                    result['withdrawn_wake_id'] = prior['wake_id']
                else:
                    result['wake_id'] = self.events._propose_handoff(ctx.principal, ctx.owner, ref,
                                                                   target_principal=payload.get('target_principal'))
                    self._attach_actor(result['wake_id'], ctx)
            if row is not None:
                current = self.events._order(row['owner'], row['order_id'])
                result.update(obligation_ref=current['order_id'], origin=self.view(current, compact=True)['origin'],
                              responsible=current['responsible'], next_check_at=current['next_check_at'],
                              lifecycle=current['lifecycle'])
            if 'wake_id' in result:
                result.update(delivery_state=self.events.wake(result['wake_id'])['state'], native_status='not_observed')
            self.feed._export(ctx.owner, ctx.device_id)
            encoded = json.dumps(result, sort_keys=True, separators=(',', ':'), allow_nan=False)
            if len(encoded.encode()) > 65536:
                raise OrderRefused('control_result_too_large')
            self.events.db.execute('INSERT INTO android_order_controls VALUES(?,?,?)', (key, digest, encoded))
            return result

    def _attach_actor(self, wake_id, ctx):
        wake = self.events.wake(wake_id)
        payload = json.loads(wake['payload']) | self._event_actor(ctx)
        self.events.db.execute('UPDATE android_order_wakes SET payload=? WHERE wake_id=?', (document(payload), wake_id))

    def _notice(self, ctx, row, kind, content, identity):
        if row['lifecycle'] != 'active':
            raise OrderRefused('order_closed')
        wake = str(uuid.uuid5(uuid.NAMESPACE_URL, 'native-notice:' + identity))
        content = content | self._event_actor(ctx)
        encoded = document(content)
        old = self.events.db.execute('SELECT payload FROM android_order_wakes WHERE wake_id=?', (wake,)).fetchone()
        if old:
            if old['payload'] != encoded:
                raise OrderRefused('notice_identity_conflict')
            return wake
        self.events.db.execute('''INSERT INTO android_order_wakes
            (wake_id,owner,order_id,device_id,kind,target,principal,through,revision,payload,
             original_request_id,native_thread_id,native_turn_id)
            VALUES(?,?,?,?,?,'grok',?,?,?,?,?,?,?)''',
            (wake, ctx.owner, row['order_id'], ctx.device_id, kind, ctx.owner, row['seen'], row['revision'], encoded,
             row['original_request_id'], ctx.native_thread_id, ctx.native_turn_id))
        return wake

    @staticmethod
    def _event_actor(ctx):
        actual = actor(ctx)
        return {'actor': actual} | {key: actual[key] for key in (
            'native_thread_id', 'native_turn_id', 'native_call_id', 'native_request_id', 'item_id', 'native_namespace')}

    def observe_native_question(self, ctx, *, item_id, message_id, questions, native_request_id=None, question_ids=None):
        self._context(ctx, call=False)
        ident(item_id, optional=True)
        ident(message_id, optional=True)
        if not item_id and not message_id:
            raise OrderRefused('native_question_identity_required')
        ids = [] if question_ids is None else question_ids
        if not isinstance(questions, list) or not 1 <= len(questions) <= 32 or not all(isinstance(q, dict) for q in questions):
            raise OrderRefused('invalid_questions')
        if type(native_request_id) not in (str, int, type(None)) or not isinstance(ids, list):
            raise OrderRefused('invalid_question_identity')
        if native_request_id is None:
            if ids:
                raise OrderRefused('async_question_has_no_rpc_ids')
        elif not ids or any(not isinstance(q, str) or not q for q in ids) or ids != [q.get('id') for q in questions] or len(set(ids)) != len(ids):
            raise OrderRefused('invalid_question_identity')
        actual = NativeOrderContext(ctx.principal, ctx.owner, ctx.device_id, ctx.task_id, ctx.request_id,
                                   ctx.native_thread_id, ctx.native_turn_id, ctx.native_call_id, native_request_id,
                                   item_id, ctx.native_namespace)
        identity = document([ctx.owner, ctx.device_id, ctx.task_id, ctx.request_id, ctx.native_thread_id,
                             ctx.native_turn_id, native_request_id, item_id, message_id, ids])
        with self.events.tx():
            existing = self.events.db.execute('SELECT 1 FROM android_order_obligations WHERE owner=? AND order_id=?',
                                              (ctx.owner, self._task_ref(actual))).fetchone()
            row = self._notice_row(actual)
            origin = json.loads(row['origin_json'])
            if existing is None:
                origin.update(message_id=message_id, question_ids=ids)
                self.events.db.execute('UPDATE android_order_obligations SET origin_json=? WHERE owner=? AND order_id=?',
                                       (document(origin), ctx.owner, row['order_id']))
                row = self.events._order(ctx.owner, row['order_id'])
            wake = self._notice(actual, row, 'question', dict(message_id=message_id, questions=questions,
                                                            question_ids=ids, source='async' if native_request_id is None else 'rpc'), identity)
            self.feed._export(ctx.owner, ctx.device_id)
            return wake
