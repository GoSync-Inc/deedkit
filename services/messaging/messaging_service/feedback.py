"""Private, bounded, exact-credential-owned feedback; never external messaging."""
from __future__ import annotations
import base64
from datetime import datetime, timedelta
import hashlib
import json
import re
from psycopg.types.json import Jsonb
from .field_errors import field_error, one_per_field
from .policy import OPERATION_PERMISSIONS, audit, scope_filter, targets


REF_KINDS = ('message_ids', 'chat_ids', 'subscription_ids')
# Disposition vocabulary (ANYM-343); migration 039 holds the same CHECK. `fixed` = merged to main,
# `verify_requested` = deployed: merged source is not live behavior. The reporting agent owns
# `verified` and `reopened` (ANYM-344), so the operator command refuses them.
STATUSES = ('received', 'accepted', 'declined', 'duplicate', 'fixed', 'verify_requested', 'verified', 'reopened')
AGENT_VERDICTS = ('verified', 'reopened')
# SemVer 2.0 (semver.org regex, ASCII classes); migration 039 holds the same pattern and the 80-character bound.
FIXED_IN = re.compile(r'deedkit@[0-9a-f]{40}|anymsg@(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-(0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)(\.(0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?(\+[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?')
SPEC_REF = re.compile(r'ANYM-[1-9][0-9]{0,5}')
# ANYM-342: `operation` names the tool as the agent sees it. Messaging keeps no copy of the other modules' tool
# lists (the gateway checks those against its catalog), so another module's tool is accepted by shape alone.
GATEWAY_PREFIX = 'messages_'
OTHER_MODULE_TOOL = re.compile(r'(calendar|calls|platform)_[a-z0-9_]{1,120}')
UUID = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')
# A submission transaction ends within this bound or is aborted, never committed late (ANYM-265). The export
# overlap minimum (2 minutes) exceeds it plus the 5 s statements that authorize the key before it starts.
SUBMIT_TRANSACTION_TIMEOUT = '60s'
OVERLAP_MINUTES = (2, 10, 1440)  # minimum, default, maximum
SUBMISSION_FIELDS = ('id', 'request_id', 'category', 'description', 'operation', 'created_at', 'principal', 'key_id')
DISPOSITION_FIELDS = ('id', 'status', 'status_note', 'fixed_in', 'spec_ref', 'duplicate_of', 'status_changed_at')


def canonical_operation(name):
    """The stored form of an `operation`, or None: messaging operations bare, other modules' tools as given."""
    if name in OPERATION_PERMISSIONS:
        return name
    if name.startswith(GATEWAY_PREFIX) and name[len(GATEWAY_PREFIX):] in OPERATION_PERMISSIONS:
        return name[len(GATEWAY_PREFIX):]
    return name if OTHER_MODULE_TOOL.fullmatch(name) else None


def _invalid(errors):
    """invalid_feedback with bounded field_errors, never an input value."""
    from .store import APIError
    failure = APIError('invalid_feedback')
    failure.field_errors = one_per_field(field_error(*e) for e in errors)
    return failure


def _ref_errors(refs):
    if not isinstance(refs, dict):
        return [('refs', 'type', 'object')]
    errors = []
    if set(refs) - set(REF_KINDS):
        errors.append(('refs', 'unknown_field', 'only the listed reference types', REF_KINDS))
    for kind in REF_KINDS:
        values = refs.get(kind)
        if kind not in refs:
            continue
        if not isinstance(values, list):
            errors.append(('refs/' + kind, 'type', 'array'))
        elif len(values) > 32:
            errors.append(('refs/' + kind, 'max_items', 'at most 32 items'))
        else:
            for index, v in enumerate(values):
                path = 'refs/%s/%d' % (kind, index)
                if not isinstance(v, str):
                    errors.append((path, 'type', 'string'))
                elif not 1 <= len(v) <= 256:
                    errors.append((path, 'min_length' if not v else 'max_length', '1–256 characters'))
                elif any(ord(ch) < 32 or ord(ch) == 127 for ch in v):
                    errors.append((path, 'pattern', 'no control characters'))
    return errors


def _validate_refs(c, p, refs):
    from .store import APIError
    if refs is None:
        refs = {}
    errors = _ref_errors(refs)
    if errors:
        raise _invalid(errors)
    normalized = {}
    for kind, values in refs.items():
        normalized[kind] = sorted(set(values))
        if not values:
            continue
        if kind == 'subscription_ids':
            # Refresh the same policy/version/source fences as a normal scoped
            # subscription read; access_changed is otherwise lazily persisted.
            from .subscriptions import _owned
            found = []
            for subscription_id in normalized[kind]:
                row = _owned(c, p, subscription_id)
                if row['state'] == 'access_changed':
                    raise APIError('not_found', 404)
                found.append({'id': str(row['id'])})
        else:
            table, alias, chat_column = ('messages', 'm', 'chat') if kind == 'message_ids' else ('chats', 'c', 'id')
            where, args = scope_filter(p, alias, chat_column)
            found = c.execute('SELECT ' + alias + '.id FROM ' + table + ' ' + alias +
                ' WHERE ' + where + ' AND ' + alias + '.id=ANY(%s)', (*args, values)).fetchall()
        if {row['id'] for row in found} != set(values):
            raise APIError('not_found', 404)
    if len(json.dumps(normalized).encode()) > 8192:
        raise _invalid([('refs', 'size', 'at most 8 KiB')])
    return normalized


def submit_feedback(store, c, p, request_id, category, description, *, operation=None, refs=None):
    from .store import APIError
    c.execute("SELECT set_config('transaction_timeout',%s,true)", (SUBMIT_TRANSACTION_TIMEOUT,))
    if 'read' not in p['permissions']:
        raise APIError('forbidden', 403)
    errors = []
    if not isinstance(request_id, str):
        errors.append(('request_id', 'type', 'string'))
    elif not re.fullmatch(r'[a-zA-Z0-9_.:-]{8,128}', request_id):
        errors.append(('request_id', 'min_length' if len(request_id) < 8 else 'max_length' if len(request_id) > 128 else 'pattern',
            '8–128 characters: letters, digits and _ . : -'))
    if category not in ('bug', 'feature', 'quality', 'other'):
        errors.append(('category', 'enum', None, ('bug', 'feature', 'quality', 'other')))
    if not isinstance(description, str):
        errors.append(('description', 'type', 'string'))
    elif not 1 <= len(description) <= 4000:
        errors.append(('description', 'min_length' if not description else 'max_length', '1–4000 characters'))
    elif not description.strip():
        errors.append(('description', 'blank', 'text with a non-space character'))
    elif any(ord(ch) < 32 and ch not in '\n\t' or ord(ch) == 127 for ch in description):
        errors.append(('description', 'pattern', 'no control characters except newline and tab'))
    if operation is not None:
        if not isinstance(operation, str):
            errors.append(('operation', 'type', 'string'))
        elif (operation := canonical_operation(operation)) is None:
            errors.append(('operation', 'enum', 'a tool name from tools/list, with or without the messages_ prefix'))
    if errors:
        raise _invalid(errors)
    refs = _validate_refs(c, p, refs)
    value = dict(category=category, description=description, operation=operation, refs=refs)
    request_hash = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('feedback:' + str(p['key_id']),))
    existing = c.execute('SELECT id,request_hash FROM agent_feedback WHERE key_id=%s AND request_id=%s',
        (p['key_id'], request_id)).fetchone()
    if existing:
        if existing['request_hash'] != request_hash:
            raise APIError('idempotency_conflict', 409)
        row = existing
    else:
        if c.execute('SELECT count(*) AS n FROM agent_feedback WHERE key_id=%s',
                (p['key_id'],)).fetchone()['n'] >= 1000:
            raise APIError('feedback_limit', 429)
        row = c.execute('''INSERT INTO agent_feedback(principal,key_id,request_id,request_hash,
            category,description,operation,refs) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
            (p['id'], p['key_id'], request_id, request_hash, category, description, operation, Jsonb(refs))).fetchone()
    targets(p, [str(row['id'])])
    return {'feedback_id': str(row['id']), 'status': 'recorded', 'external_delivery': False}


def list_feedback(store, c, p, *, limit=50, status=None):
    from .store import bounded, serial
    bounded(limit)
    if status is not None and status not in STATUSES:
        raise _invalid([('status', 'enum', None, STATUSES)])
    rows = c.execute('''SELECT id,category,description,operation,created_at,status,status_note,fixed_in,
        spec_ref,duplicate_of,status_changed_at FROM agent_feedback
        WHERE principal=%s AND key_id=%s AND (%s::text IS NULL OR status=%s)
        ORDER BY created_at DESC,id DESC LIMIT %s''',
        (p['id'], p['key_id'], status, status, limit + 1)).fetchall()
    # Source references are validated on submission, then omitted from listing so
    # a later policy change cannot leak formerly authorized source identifiers.
    targets(p, [str(row['id']) for row in rows[:limit]])
    return serial({'items': rows[:limit], 'has_more': len(rows) > limit, 'external_delivery': False})


def _status_errors(ids, status, fixed_in, spec_ref, duplicate_of, note):
    """Operator-side refusal codes, never an input value."""
    errors = []
    if not isinstance(ids, (list, tuple)) or not 1 <= len(ids) <= 100:
        errors.append('feedback_ids_1_to_100')
    elif not all(isinstance(value, str) and UUID.fullmatch(value) for value in ids):
        errors.append('invalid_feedback_id')
    if status in AGENT_VERDICTS:
        errors.append('status_reserved_for_reporting_agent')
    elif status not in STATUSES:
        errors.append('invalid_status')
    if fixed_in is not None and not (isinstance(fixed_in, str) and len(fixed_in) <= 80 and FIXED_IN.fullmatch(fixed_in)):
        errors.append('invalid_fixed_in')
    if spec_ref is not None and not (isinstance(spec_ref, str) and SPEC_REF.fullmatch(spec_ref)):
        errors.append('invalid_spec_ref')
    if duplicate_of is not None and not (isinstance(duplicate_of, str) and UUID.fullmatch(duplicate_of)):
        errors.append('invalid_duplicate_of')
    if note is not None and not (isinstance(note, str) and 1 <= len(note) <= 280 and note.strip() and
            not any(ord(ch) < 32 or ord(ch) == 127 for ch in note)):
        errors.append('invalid_status_note')
    if status in ('accepted', 'fixed', 'verify_requested') and spec_ref is None:
        errors.append('spec_ref_required')
    if status in ('fixed', 'verify_requested') and fixed_in is None:
        errors.append('fixed_in_required')
    if status not in ('fixed', 'verify_requested') and fixed_in is not None:
        errors.append('fixed_in_only_for_fixed_or_verify_requested')
    if status == 'duplicate' and duplicate_of is None and spec_ref is None:
        errors.append('duplicate_of_or_spec_ref_required')
    if status != 'duplicate' and duplicate_of is not None:
        errors.append('duplicate_of_only_for_duplicate')
    return errors


def set_feedback_status(c, ids, status, *, fixed_in=None, spec_ref=None, duplicate_of=None, note=None,
                        unchanged_since=None, not_after=None):
    """Operator-only disposition (ANYM-343) on the owner connection, never an agent key or MCP tool.

    A call replaces the whole disposition of every named item atomically, or changes nothing. With
    `unchanged_since` (ANYM-473) it changes nothing when any named item's disposition changed after that time,
    so a plan read from an export never overwrites a newer disposition; an item never set passes. With `not_after`
    it changes nothing once the database clock is past that time, so a call whose reply was lost commits by
    not_after plus the 5 s bound or never.
    """
    errors = _status_errors(ids, status, fixed_in, spec_ref, duplicate_of, note)
    if errors:
        raise ValueError(','.join(errors))
    if unchanged_since is not None:
        unchanged_since = _timestamp(unchanged_since, 'invalid_unchanged_since')
    if not_after is not None:
        not_after = _timestamp(not_after, 'invalid_not_after')
    ids = sorted({value.lower() for value in ids})
    # The same 5 s bound as an agent submission's statements, so a status change never holds
    # agent_feedback row locks longer than a submission does.
    c.execute("SET LOCAL transaction_timeout='5s'")
    rows = c.execute('SELECT id,principal,key_id,status_changed_at FROM agent_feedback WHERE id=ANY(%s::uuid[]) '
        'ORDER BY id FOR UPDATE', (ids,)).fetchall()
    if len(rows) != len(ids):
        raise ValueError('unknown_feedback: %d of %d IDs' % (len(ids) - len(rows), len(ids)))
    if unchanged_since is not None:
        changed = sum(row['status_changed_at'] is not None and row['status_changed_at'] > unchanged_since for row in rows)
        if changed:
            raise ValueError('changed_since_plan: %d of %d IDs' % (changed, len(ids)))
    if not_after is not None and c.execute('SELECT clock_timestamp() > %s AS late', (not_after,)).fetchone()['late']:
        raise ValueError('not_after_passed')
    if duplicate_of is not None:
        duplicate_of = duplicate_of.lower()
        if duplicate_of in ids:
            raise ValueError('duplicate_of_names_an_updated_item')
        target = c.execute('SELECT key_id FROM agent_feedback WHERE id=%s', (duplicate_of,)).fetchone()
        if target is None or any(row['key_id'] != target['key_id'] for row in rows):
            raise ValueError('duplicate_of_not_same_key')
    changed = c.execute('''UPDATE agent_feedback SET status=%s,status_note=%s,fixed_in=%s,spec_ref=%s,
        duplicate_of=%s,status_changed_at=clock_timestamp() WHERE id=ANY(%s::uuid[]) RETURNING id''',
        (status, note, fixed_in, spec_ref, duplicate_of, ids)).fetchall()
    # One content-free operator audit row per item: the fixed operation code and the feedback ID only.
    for row in rows:
        audit(c, {'id': row['principal'], 'key_id': row['key_id'], '_audit_targets': [str(row['id'])]},
            'set_feedback_status', 'ok')
    return {'updated': len(changed), 'status': status, 'feedback_ids': sorted(str(row['id']) for row in changed)}


def _timestamp(value, code):
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) and len(value) <= 64 else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise ValueError(code)
    return parsed


def _page_token(mode, head, row):
    position = {'v': 1, 'm': mode, 'h': head.isoformat(), 't': row[mode_column(mode)].isoformat(), 'i': str(row['id'])}
    return base64.urlsafe_b64encode(json.dumps(position, separators=(',', ':')).encode()).decode().rstrip('=')


def mode_column(mode):
    return 'created_at' if mode == 'created' else 'status_changed_at'


def _read_page_token(token):
    try:
        raw = base64.urlsafe_b64decode(token + '=' * (-len(token) % 4)) if isinstance(token, str) and len(token) <= 512 else b''
        position = json.loads(raw)
        if not (isinstance(position, dict) and set(position) == {'v', 'm', 'h', 't', 'i'} and position['v'] == 1
                and position['m'] in ('created', 'changed') and isinstance(position['i'], str) and UUID.fullmatch(position['i'])):
            raise ValueError
        return position['m'], _timestamp(position['h'], 'invalid_page'), _timestamp(position['t'], 'invalid_page'), position['i']
    except ValueError:
        raise ValueError('invalid_page') from None


def export_feedback(c, *, since=None, changed_since=None, page=None, overlap=None, limit=50):
    """Owner export of every principal's feedback (ANYM-265): operator CLI only, read-only, keyset-paged.

    `created` mode pages submissions with their current dispositions by (created_at, id); `changed` mode pages
    dispositions by (status_changed_at, id). Every page of one export is bounded by `export_head`, fixed by the
    first page. Pages are separate transactions, not a snapshot: a row committed late behind the cursor is returned
    by the next export, whose start is `since` minus the overlap; the caller deduplicates by feedback ID.
    """
    from .store import bounded, serial
    c.execute('SET TRANSACTION READ ONLY')
    bounded(limit)
    if sum(value is not None for value in (since, changed_since, page)) > 1:
        raise ValueError('since_changed_since_and_page_are_exclusive')
    if overlap is not None and since is None and changed_since is None:
        raise ValueError('overlap_needs_since_or_changed_since')
    overlap = OVERLAP_MINUTES[1] if overlap is None else overlap
    if isinstance(overlap, bool) or not isinstance(overlap, int) or not OVERLAP_MINUTES[0] <= overlap <= OVERLAP_MINUTES[2]:
        raise ValueError('invalid_overlap')
    lower = after = None
    if page is not None:
        mode, head, *after = _read_page_token(page)
    else:
        mode = 'changed' if changed_since is not None else 'created'
        start = since if mode == 'created' else changed_since
        lower = None if start is None else _timestamp(start, 'invalid_since') - timedelta(minutes=overlap)
        head = c.execute('SELECT clock_timestamp() AS head').fetchone()['head']
    column = mode_column(mode)
    fields = SUBMISSION_FIELDS + DISPOSITION_FIELDS[1:] if mode == 'created' else DISPOSITION_FIELDS
    rows = c.execute('SELECT ' + ','.join(fields) + ' FROM agent_feedback WHERE ' + column + ' IS NOT NULL AND ' +
        column + '<=%(head)s AND (%(lower)s::timestamptz IS NULL OR ' + column + '>=%(lower)s) AND ' +
        '(%(after)s::timestamptz IS NULL OR (' + column + ',id)>(%(after)s,%(after_id)s::uuid)) ORDER BY ' + column +
        ',id LIMIT %(limit)s', {'head': head, 'lower': lower, 'after': after[0] if after else None,
        'after_id': after[1] if after else None, 'limit': limit + 1}).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]
    result = {'untrusted_content': True, 'mode': mode, 'export_head': head,
              'dispositions': [{name: row[name] for name in DISPOSITION_FIELDS} for row in rows],
              'has_more': has_more, 'next_page': _page_token(mode, head, rows[-1]) if has_more else None}
    if mode == 'created':
        result['submissions'] = [{name: row[name] for name in SUBMISSION_FIELDS} for row in rows]
    return serial(result)
