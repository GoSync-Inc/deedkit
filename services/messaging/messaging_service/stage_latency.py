"""ANYM-865: how many seconds each archive stage takes over a window, and what waits for each stage now.

get_sync_status(include_latency=true) and the operator's `sync-latency` table read this. Every value is a count or
seconds next to an account, stage, origin, chat type, message kind and hint policy: no chat IDs, titles, senders or
bodies. Each stage group is one read-only statement under its own statement timeout and savepoint; a group that
fails names its stages in `latency_unavailable` and leaves the rest of the caller's transaction as it was.

Stage times are the commit times the journal already keeps: `events.created_at` (the inserting transaction's
start), `subscription_matches.matched_at` and the first delivered hint's `subscription_outbox.finished_at`.
"""
from datetime import timedelta
import logging

import psycopg

from .media_policy import ELIGIBILITY_JOIN, recognition_withheld

log = logging.getLogger(__name__)

WINDOWS = (1, 24, 72, 168)
DEFAULT_WINDOW = 24
# Each stage group's statement timeout by window (criterion 4); the caller's own timeout is restored after it.
TIMEOUTS = {1: '2s', 24: '2s', 72: '5s', 168: '5s'}
STAGES = ('arrival', 'download', 'text', 'transcript', 'description', 'failed', 'match', 'hint')
# events has no created_at index: the window starts at the first event id committed at most this long before it
# (a binary search over the primary key). created_at rises with id except within one transaction's length.
FLOOR_SLACK = timedelta(hours=1)
ROW_KEYS = ('account', 'stage', 'origin', 'chat_type', 'message_kind', 'policy', 'n', 'p50_s', 'p90_s', 'window_h')
# Percentile columns of every statement; a pending-view row leaves them NULL and fills the four after them.
_P = 'percentile_cont({q}) WITHIN GROUP (ORDER BY {d})'
CHAT_TYPE = '''coalesce(CASE WHEN ch.chat_type IN ('private','group','channel') THEN ch.chat_type
    WHEN lcc.peer_kind='user' THEN 'private' WHEN lcc.peer_kind='chat' THEN 'group'
    WHEN lcc.peer_kind='channel' THEN 'channel' END,'unknown')'''
CHAT_JOINS = '''LEFT JOIN chats ch ON ch.id=m.chat
    LEFT JOIN collector_chats lcc ON lcc.account=m.account AND lcc.provider_chat=ch.provider_chat'''
# A service message (create, rename, join, pin notice) is its own arrival row: its metadata's service_kind when a
# collector records one, else `service`.
MESSAGE_KIND = '''CASE WHEN e.snapshot->'metadata'->'is_service'='true'::jsonb
        OR e.snapshot->'metadata'->>'message_kind'='system'
    THEN coalesce(nullif(e.snapshot->'metadata'->>'service_kind',''),'service') ELSE 'message' END'''
# A delivery policy that waits on purpose: debounce, a batch or quiet hours (delivery_policy._due and _gate).
HELD_POLICY = '''(jsonb_typeof(d.policy->'debounce_s')='number' AND (d.policy->>'debounce_s')::numeric>0
    OR jsonb_typeof(d.policy->'batch_window_s')='number' AND (d.policy->>'batch_window_s')::numeric>0
    OR jsonb_typeof(d.policy->'batch_count')='number' AND (d.policy->>'batch_count')::numeric>0
    OR jsonb_typeof(d.policy->'quiet_hours')='object')'''
HINT_FINAL = "('delivered','dead_letter','cancelled')"


def valid_window(value):
    return type(value) is int and value in WINDOWS


def _seconds(value):
    return None if value is None else round(float(value), 3)


def _scope(p):
    """The caller's archive scope on messages alias m, as policy.scope_filter states it (named parameters)."""
    return 'm.account=ANY(%(accounts)s)' + ('' if p['chat_ids'] is None else ' AND m.chat=ANY(%(chats)s)')


def _floor(c, target):
    """The first event id whose created_at is at or after target, by binary search over the primary key."""
    bounds = c.execute('SELECT min(id) AS lo,max(id) AS hi FROM events').fetchone()
    if bounds['lo'] is None:
        return 0
    lo, hi = bounds['lo'], bounds['hi'] + 1
    while lo < hi:
        mid = (lo + hi) // 2
        row = c.execute('SELECT id,created_at FROM events WHERE id>=%s ORDER BY id LIMIT 1', (mid,)).fetchone()
        if row is None or row['created_at'] >= target:
            hi = mid
        else:
            lo = row['id'] + 1
    return lo


def _arrival():
    delta = "greatest(0,extract(epoch FROM e.created_at-m.sent_at))::float8"
    return '''SELECT 'p' AS row,m.account,'arrival' AS stage,e.origin,''' + CHAT_TYPE + ''' AS chat_type,
        ''' + MESSAGE_KIND + ''' AS message_kind,NULL AS policy,count(*) AS n,
        ''' + _P.format(q=0.5, d=delta) + ' AS p50,' + _P.format(q=0.9, d=delta) + ''' AS p90
    FROM events e JOIN messages m ON m.id=e.message ''' + CHAT_JOINS + '''
    WHERE e.id>=%(floor)s AND e.created_at>=%(cutoff)s AND e.kind='message.created' AND e.origin='live'
        AND {scope}
    GROUP BY 2,4,5,6'''


def _download():
    delta = 'extract(epoch FROM e.created_at-created.at)::float8'
    return '''SELECT 'p' AS row,m.account,'download' AS stage,e.origin,''' + CHAT_TYPE + ''' AS chat_type,
        NULL AS message_kind,NULL AS policy,count(*) AS n,
        ''' + _P.format(q=0.5, d=delta) + ' AS p50,' + _P.format(q=0.9, d=delta) + ''' AS p90,
        NULL::bigint AS pending_n,NULL::float8 AS oldest_s,NULL::bigint AS on_request_n
    FROM events e JOIN messages m ON m.id=e.message ''' + CHAT_JOINS + '''
    CROSS JOIN LATERAL (SELECT min(x.created_at) AS at FROM events x
        WHERE x.message=e.message AND x.kind='message.created') created
    WHERE e.id>=%(floor)s AND e.created_at>=%(cutoff)s AND e.kind='attachment.ready' AND created.at IS NOT NULL
        AND {scope}
    GROUP BY 2,4,5
    UNION ALL
    SELECT 'now',m.account,'download',NULL,NULL,NULL,NULL,NULL,NULL,NULL,
        count(*) FILTER (WHERE el.claim NOT IN ''' + _ON_REQUEST + '''),
        extract(epoch FROM %(now)s-min(j.created_at) FILTER (WHERE el.claim NOT IN ''' + _ON_REQUEST + '''))::float8,
        count(*) FILTER (WHERE el.claim IN ''' + _ON_REQUEST + ''')
    FROM media_jobs j JOIN messages m ON m.id=j.message AND m.account=j.account
    JOIN chats ch ON ch.id=m.chat AND ch.account=m.account ''' + ELIGIBILITY_JOIN + '''
    LEFT JOIN attachments a ON a.id=j.id AND a.message=j.message AND a.active
    WHERE j.state IN ('queued','leased') AND a.id IS NULL AND NOT m.deleted AND {scope}
    GROUP BY 2'''


# Queued downloads that wait for a request (request_file_download) by policy, never by a stall.
_ON_REQUEST = ("('group_chat_on_request','channel_on_request','outside_prefetch_window','outside_history_bounds',"
               "'collection_excluded')")
# The original's own attachment.ready commit; a legacy original without one falls back to its row's last change.
_READY_AT = '''(SELECT max(r.created_at) FROM events r WHERE r.message=a.message AND r.kind='attachment.ready'
    AND r.snapshot->'processing'->>'attachment_id'=a.id)'''


def _files(withheld):
    delta = 'extract(epoch FROM e.created_at-ready.at)::float8'
    stage = '''CASE e.kind WHEN 'file.text.ready' THEN 'text' WHEN 'file.transcript.ready' THEN 'transcript'
        WHEN 'file.description.ready' THEN 'description' ELSE 'failed' END'''
    waiting = '''SELECT 'now',m.account,{stage},NULL,NULL,NULL,NULL,NULL,NULL,NULL,
        count(*) FILTER (WHERE NOT w.withheld),
        extract(epoch FROM %(now)s-min(coalesce(''' + _READY_AT + ''',q.updated_at)) FILTER (WHERE NOT w.withheld))::float8,
        count(*) FILTER (WHERE w.withheld)
    FROM {table} q JOIN attachments a ON a.id=q.attachment AND a.active JOIN messages m ON m.id=a.message
    CROSS JOIN LATERAL (SELECT ''' + withheld + ''' AS withheld) w
    WHERE q.state IN ('queued','leased') AND q.source_digest=a.digest AND q.source_revision=m.revision
        AND NOT m.deleted AND {scope}
    GROUP BY 2,3'''
    return ('''SELECT 'p' AS row,m.account,''' + stage + ''' AS stage,e.origin,''' + CHAT_TYPE + ''' AS chat_type,
        NULL AS message_kind,NULL AS policy,count(*) AS n,
        ''' + _P.format(q=0.5, d=delta) + ' AS p50,' + _P.format(q=0.9, d=delta) + ''' AS p90,
        NULL::bigint AS pending_n,NULL::float8 AS oldest_s,NULL::bigint AS on_request_n
    FROM events e JOIN messages m ON m.id=e.message ''' + CHAT_JOINS + '''
    CROSS JOIN LATERAL (SELECT max(r.created_at) AS at FROM events r WHERE r.message=e.message
        AND r.kind='attachment.ready' AND r.created_at<=e.created_at
        AND r.snapshot->'processing'->>'attachment_id'=e.snapshot->'processing'->>'attachment_id') ready
    WHERE e.id>=%(floor)s AND e.created_at>=%(cutoff)s
        AND e.kind IN ('file.text.ready','file.transcript.ready','file.description.ready','file.processing.failed')
        AND (e.kind='file.processing.failed' OR ready.at IS NOT NULL) AND {scope}
    GROUP BY 2,3,4,5
    UNION ALL ''' + waiting.replace('{stage}', "'text'").replace('{table}', 'attachment_text') + '''
    UNION ALL ''' + waiting.replace('{stage}', "CASE q.kind WHEN 'audio' THEN 'transcript' ELSE 'description' END")
        .replace('{table}', 'attachment_enrichment'))


def _subscriptions(owner):
    delta_match = 'extract(epoch FROM matched_at-created_at)::float8'
    delta_hint = 'extract(epoch FROM h.finished-matched_at)::float8'
    who = 's.principal=%(principal)s AND s.key_id=%(key_id)s' if owner else 'true'
    return '''WITH own AS (SELECT s.id,s.state,coalesce(s.start_position,0) AS start_position,
            d.subscription IS NOT NULL AND (d.receiver IS NOT NULL OR EXISTS(SELECT FROM delivery_endpoints de
                WHERE de.id=d.endpoint AND de.state='enabled')) AS expects_hints,
            CASE WHEN ''' + HELD_POLICY + ''' THEN 'held_by_policy' ELSE 'immediate' END AS policy
        FROM subscriptions s LEFT JOIN subscription_delivery d ON d.subscription=s.id WHERE ''' + who + '''),
    matched AS (SELECT own.id AS subscription,own.policy,sm.event,sm.matched_at,e.account,e.origin,e.created_at
        FROM own JOIN subscription_matches sm ON sm.subscription=own.id JOIN events e ON e.id=sm.event
        WHERE sm.event>=%(floor)s AND sm.matched_at>=%(cutoff)s AND NOT sm.hidden AND e.account=ANY(%(accounts)s)),
    waiting AS (SELECT sm.matched_at,e.account,
            coalesce(w.held_reason IN ('debounce','batch','quiet_hours') AND w.due_at>%(now)s
                AND sm.event>coalesce(w.hinted_through,own.start_position),false) AS held
        FROM own LEFT JOIN subscription_hint_windows w ON w.subscription=own.id
        CROSS JOIN LATERAL (SELECT coalesce(max(o.through_position),0) AS through FROM subscription_outbox o
            WHERE o.subscription=own.id AND o.kind='events' AND o.state IN ''' + HINT_FINAL + ''') final
        JOIN subscription_matches sm ON sm.subscription=own.id AND sm.event>greatest(final.through,own.start_position)
        JOIN events e ON e.id=sm.event
        WHERE own.expects_hints AND own.state='active' AND NOT sm.hidden AND e.account=ANY(%(accounts)s))
    SELECT 'p' AS row,account,'match' AS stage,origin,NULL AS chat_type,NULL AS message_kind,NULL AS policy,
        count(*) AS n,''' + _P.format(q=0.5, d=delta_match) + ' AS p50,' + _P.format(q=0.9, d=delta_match) + ''' AS p90,
        NULL::bigint AS pending_n,NULL::float8 AS oldest_s,NULL::bigint AS held_n
    FROM matched GROUP BY 2,4
    UNION ALL
    SELECT 'p',account,'hint',origin,NULL,NULL,policy,count(*),
        ''' + _P.format(q=0.5, d=delta_hint) + ',' + _P.format(q=0.9, d=delta_hint) + ''',NULL,NULL,NULL
    FROM matched CROSS JOIN LATERAL (SELECT min(o.finished_at) AS finished FROM subscription_outbox o
        WHERE o.subscription=matched.subscription AND o.kind='events' AND o.state='delivered'
            AND o.through_position>=matched.event) h
    WHERE h.finished IS NOT NULL GROUP BY 2,4,7
    UNION ALL
    SELECT 'now',account,'hint',NULL,NULL,NULL,NULL,NULL,NULL,NULL,count(*) FILTER (WHERE NOT held),
        extract(epoch FROM %(now)s-min(matched_at) FILTER (WHERE NOT held))::float8,count(*) FILTER (WHERE held)
    FROM waiting GROUP BY 2'''


def _bounded(c, timeout, run):
    """run() under its own savepoint, statement timeout and jit=off; the session's timeout is restored after."""
    with c.transaction():
        previous = c.execute("SELECT current_setting('statement_timeout') AS v").fetchone()['v']
        c.execute("SELECT set_config('statement_timeout',%s,true),set_config('jit','off',true)", (timeout,))
        result = run()
        c.execute("SELECT set_config('statement_timeout',%s,true)", (previous,))
    return result


def read(c, p, window_h=DEFAULT_WINDOW, owner=None):
    """{latency, latency_window_h, latency_pending, latency_unavailable} for scope p (accounts, chat_ids).

    owner=(principal, key_id) limits match and hint rows to that key's own subscriptions; None (the operator's
    table) reads every subscription.
    """
    if not valid_window(window_h):
        raise ValueError('invalid_latency_window')
    timeout = TIMEOUTS[window_h]
    result = {'latency': [], 'latency_window_h': window_h, 'latency_pending': [], 'latency_unavailable': []}

    def start():
        now = c.execute('SELECT clock_timestamp() AS t').fetchone()['t']
        return now, _floor(c, now - timedelta(hours=window_h) - FLOOR_SLACK), recognition_withheld(c)

    try:
        now, floor, withheld = _bounded(c, timeout, start)
        cutoff = now - timedelta(hours=window_h)
    except psycopg.Error as error:
        log.warning('stage_latency_unavailable group=floor exception_type=%s', type(error).__name__)
        result['latency_unavailable'] = list(STAGES)
        return result
    args = {'accounts': list(p['accounts']), 'chats': p['chat_ids'], 'floor': floor, 'cutoff': cutoff, 'now': now,
            'principal': owner[0] if owner else None, 'key_id': owner[1] if owner else None}
    scope = _scope(p)
    # Fragments such as ELIGIBILITY_JOIN hold regex braces: placeholders are replaced, never str.format'ed.
    groups = (('arrival', ('arrival',), _arrival().replace('{scope}', scope)),
              ('download', ('download',), _download().replace('{scope}', scope)),
              ('files', ('text', 'transcript', 'description', 'failed'), _files(withheld).replace('{scope}', scope)),
              ('subscriptions', ('match', 'hint'), _subscriptions(owner)))
    for name, stages, sql in groups:
        try:
            rows = _bounded(c, timeout, lambda: c.execute(sql, args).fetchall())
        except psycopg.Error as error:
            # A timeout or any database error degrades only this group's stages (criterion 4).
            log.warning('stage_latency_unavailable group=%s exception_type=%s', name, type(error).__name__)
            result['latency_unavailable'].extend(stages)
            continue
        for row in rows:
            if row['row'] == 'p':
                result['latency'].append({'account': row['account'], 'stage': row['stage'], 'origin': row['origin'],
                    'chat_type': row['chat_type'], 'message_kind': row['message_kind'], 'policy': row['policy'],
                    'n': row['n'], 'p50_s': None if row['stage'] == 'failed' else _seconds(row['p50']),
                    'p90_s': None if row['stage'] == 'failed' else _seconds(row['p90']), 'window_h': window_h})
                continue
            extra = row.get('held_n') if row['stage'] == 'hint' else row.get('on_request_n')
            if not row['pending_n'] and not extra:
                continue
            item = {'account': row['account'], 'stage': row['stage'],
                    'pending': {'n': row['pending_n'], 'oldest_s': _seconds(row['oldest_s'])}}
            item['held_n' if row['stage'] == 'hint' else 'on_request_n'] = extra or 0
            result['latency_pending'].append(item)
    order = {stage: n for n, stage in enumerate(STAGES)}
    result['latency'].sort(key=lambda r: (r['account'], order[r['stage']], *(r[k] or '' for k in
        ('origin', 'chat_type', 'message_kind', 'policy'))))
    result['latency_pending'].sort(key=lambda r: (r['account'], order[r['stage']]))
    return result


def table(result):
    """The operator's one table: percentile rows, then what waits now (blank where a column does not apply)."""
    head = ('account', 'stage', 'origin', 'chat_type', 'message_kind', 'policy', 'n', 'p50_s', 'p90_s',
            'pending_n', 'oldest_s', 'on_request_n', 'held_n')
    lines: list[tuple] = [head]
    for row in result['latency']:
        lines.append(tuple(row[k] for k in head[:9]) + (None,) * 4)
    for row in result['latency_pending']:
        lines.append((row['account'], row['stage']) + (None,) * 7 + (row['pending']['n'], row['pending']['oldest_s'],
                     row.get('on_request_n'), row.get('held_n')))
    cells = [['' if v is None else str(v) for v in line] for line in lines]
    widths = [max(len(line[i]) for line in cells) for i in range(len(head))]
    text = ['  '.join(v.ljust(w) for v, w in zip(line, widths)).rstrip() for line in cells]
    text.append(f"window_h={result['latency_window_h']}"
                + (' unavailable=' + ','.join(result['latency_unavailable']) if result['latency_unavailable'] else ''))
    return '\n'.join(text)
