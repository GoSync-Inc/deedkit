"""Immutable, attributed review pointers. No ACK, provider receipt or task change.

The gateway verifies all declared provenance. A result reference is untrusted
data, never fetched here; the destination must authorize its own result content.
"""
import re
from urllib.parse import urlsplit
from uuid import UUID

from .policy import scope_filter, targets


def invalid(code='invalid_review', status=400):
    from .store import APIError
    return APIError(code, status)


def uuid_value(value):
    if not isinstance(value, str):
        raise invalid()
    try:
        return str(UUID(value))
    except ValueError:
        raise invalid() from None


def reference(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 2048 or
            any(ord(c) <= 32 or ord(c) == 127 for c in value) or '\\' in value):
        raise invalid()
    try:
        url = urlsplit(value)
        if url.scheme == 'https' and url.hostname and url.username is None and url.password is None:
            # Validate malformed brackets/ports too; no network or URL resolution.
            _ = url.port
            return value
        # Canonical 8-4-4-4-12 text only: UUID() alone ignores where the hyphens are.
        if re.fullmatch(r'codex://threads/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}', value):
            uuid_value(url.path.removeprefix('/'))
            return value
        if re.fullmatch(r'urn:personal-os:(project|task|record):[a-zA-Z0-9_.-]{1,128}', value):
            return value
    except ValueError:
        pass
    raise invalid()


def sources(message_id, revision, evidence):
    if evidence is None:
        evidence = []
    if not isinstance(evidence, list) or len(evidence) > 19:
        raise invalid()
    result = {}
    for item in [{'message_id': message_id, 'revision': revision}, *evidence]:
        if not isinstance(item, dict) or set(item) != {'message_id', 'revision'}:
            raise invalid()
        mid, rev = item['message_id'], item['revision']
        if (not isinstance(mid, str) or not 1 <= len(mid) <= 256 or
                any(ord(c) < 32 or ord(c) == 127 for c in mid) or
                type(rev) is not int or not 1 <= rev <= 2147483647):
            raise invalid()
        if mid in result and result[mid] != rev:
            raise invalid()
        result[mid] = rev
    return sorted(result.items())


def visible(p):
    # Filter BEFORE pagination/counts/cursor lookup. Anchor is always also a
    # source. Missing provenance fails closed rather than exposing the pointer.
    scope, args = scope_filter(p, 'm')
    return ('''EXISTS(SELECT 1 FROM review_sources s WHERE s.review=r.id
                AND s.message=r.message AND s.revision=r.revision)
        AND NOT EXISTS(SELECT 1 FROM review_sources s LEFT JOIN messages m ON m.id=s.message
            WHERE s.review=r.id AND (m.id IS NULL OR NOT (''' + scope + ')))', args)


def display(c, rows):
    if not rows:
        return []
    evidence = c.execute('''SELECT s.review,s.message AS message_id,s.revision,
        m.revision AS current_revision,m.deleted FROM review_sources s
        JOIN messages m ON m.id=s.message WHERE s.review=ANY(%s) ORDER BY s.message''',
        ([r['id'] for r in rows],)).fetchall()
    grouped = {r['id']: [] for r in rows}
    for item in evidence:
        rid = item.pop('review')
        item['stale'] = item['revision'] != item['current_revision'] or item['deleted']
        grouped[rid].append(item)
    return [{**r, 'evidence': grouped[r['id']],
             'stale': any(s['stale'] for s in grouped[r['id']]),
             'assertion': 'agent_reported_review'} for r in rows]


COLUMNS = 'r.id,r.principal AS author,r.message AS message_id,r.revision,r.result_ref,r.created_at'


def list_reviews(c, p, message_id, after='', limit=20):
    from .store import bounded
    bounded(limit)
    if not isinstance(after, str):
        raise invalid('not_found', 404)
    where, args = visible(p)
    where = 'r.message=%s AND ' + where
    args = [message_id, *args]
    if after:
        try:
            cursor = uuid_value(after)
        except Exception:
            raise invalid('not_found', 404) from None
        anchor = c.execute('SELECT r.position FROM reviews r WHERE r.id=%s AND ' + where,
                           [cursor, *args]).fetchone()
        if not anchor:
            raise invalid('not_found', 404)
        where += ' AND r.position>%s'
        args.append(anchor['position'])
    rows = c.execute('SELECT ' + COLUMNS + ' FROM reviews r WHERE ' + where +
                     ' ORDER BY r.position LIMIT %s', [*args, limit + 1]).fetchall()
    return {'items': display(c, rows[:limit]),
            'next_cursor': rows[limit-1]['id'] if len(rows) > limit else None}


def record(c, p, message_id, revision, result_ref, request_id, evidence):
    if 'read' not in p['permissions']:
        raise invalid('forbidden', 403)
    request_id = uuid_value(request_id)
    result_ref = reference(result_ref)
    declared = sources(message_id, revision, evidence)
    where, args = scope_filter(p, 'm')
    authorized = c.execute('''SELECT m.id FROM messages m WHERE m.id=ANY(%s) AND ''' + where,
                           [[mid for mid, _ in declared], *args]).fetchall()
    if len(authorized) != len(declared):
        raise invalid('not_found', 404)
    revisions = c.execute('''SELECT message,revision FROM message_revisions
        WHERE message=ANY(%s) AND revision=ANY(%s)''',
        [[mid for mid, _ in declared], [rev for _, rev in declared]]).fetchall()
    if not set(declared).issubset({(r['message'], r['revision']) for r in revisions}):
        raise invalid('revision_not_found', 409)
    c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
              ('review:' + p['id'] + ':' + request_id,))
    old = c.execute('SELECT * FROM reviews WHERE principal=%s AND request_id=%s',
                    (p['id'], request_id)).fetchone()
    if old:
        saved = c.execute('SELECT message,revision FROM review_sources WHERE review=%s ORDER BY message',
                          (old['id'],)).fetchall()
        if ((old['message'], old['revision'], old['result_ref']) != (message_id, revision, result_ref)
                or [(r['message'], r['revision']) for r in saved] != declared):
            raise invalid('review_conflict', 409)
        rid = old['id']
    else:
        # Serialize publishing through commit, just like the message feed. A
        # sequence/timestamp could let a later commit disappear behind a cursor.
        position = c.execute('UPDATE review_counter SET value=value+1 WHERE id=1 RETURNING value').fetchone()['value']
        rid = c.execute('''INSERT INTO reviews(position,principal,request_id,message,revision,result_ref)
            VALUES(%s,%s,%s,%s,%s,%s) RETURNING id''',
            (position, p['id'], request_id, message_id, revision, result_ref)).fetchone()['id']
        for mid, rev in declared:
            c.execute('INSERT INTO review_sources(review,message,revision) VALUES(%s,%s,%s)', (rid, mid, rev))
    targets(p, [message_id, str(rid)])
    row = c.execute('SELECT ' + COLUMNS + ' FROM reviews r WHERE r.id=%s', (rid,)).fetchone()
    return {'review': display(c, [row])[0], 'replayed': bool(old)}
