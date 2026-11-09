"""ANYM-865: how many seconds each archive stage takes over a window, and what waits for each stage now.

get_sync_status(include_latency=true) and the operator's `sync-latency` table read this. Every value is a count or
seconds next to an account, stage, origin, chat type, message kind and hint policy: no chat IDs, titles, senders or
bodies. Each stage group is one read-only statement in its own savepoint under a statement timeout that the whole
block shares; a group that fails or finds the budget spent names its stages in `latency_unavailable` and leaves the
rest of the caller's transaction as it was.

Stage times are the commit times the journal already keeps: `events.created_at` (the inserting transaction's
start; the events_created_at index serves the window), `subscription_matches.matched_at` and the first delivered
hint's `subscription_outbox.finished_at`.
"""
from datetime import timedelta
import logging
import time

import psycopg

from .coverage import on_request_set
from .media_policy import ELIGIBILITY_JOIN, recognition_withheld
from .subscriptions import CLOSED_RETENTION_DAYS

log = logging.getLogger(__name__)

WINDOWS = (1, 24, 72, 168)
DEFAULT_WINDOW = 24
# The whole block's budget by window (criterion 4): each statement's timeout is what is left of it, so the block
# holds an API pool connection for at most this long beyond the summary. The caller's own timeout is restored after.
TIMEOUTS = {1: 2.0, 24: 2.0, 72: 5.0, 168: 5.0}
STAGES = ('arrival', 'download', 'text', 'transcript', 'description', 'failed', 'match', 'hint')
ROW_KEYS = ('account', 'stage', 'origin', 'chat_type', 'message_kind', 'policy', 'n', 'p50_s', 'p90_s', 'window_h')
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
# Queued downloads that wait for request_file_download by policy, never by a stall (media_policy.ELIGIBILITY_JOIN).
ON_REQUEST_CLAIMS = ('group_chat_on_request', 'channel_on_request', 'outside_prefetch_window', 'outside_history_bounds',
                     'collection_excluded')
# A claim the collector does not make without a confirmed policy or history bound (archive.claim_media): neither
# due work nor a request wait.
UNKNOWN_CLAIMS = ('unknown', 'history_bound_unknown')
_ON_REQUEST = '(' + ','.join("'" + claim + "'" for claim in ON_REQUEST_CLAIMS) + ')'
_UNKNOWN = '(' + ','.join("'" + claim + "'" for claim in UNKNOWN_CLAIMS) + ')'
# The original's own attachment.ready commit; a legacy original without one falls back to its row's last change.
_READY_AT = '''(SELECT max(r.created_at) FROM events r WHERE r.message=a.message AND r.kind='attachment.ready'
    AND r.snapshot->'processing'->>'attachment_id'=a.id)'''


def valid_window(value):
    return type(value) is int and value in WINDOWS


def _seconds(value):
    return None if value is None else round(float(value), 3)


def _scope(p):
    """The caller's archive scope on messages alias m, as policy.scope_filter states it (named parameters)."""
    return 'm.account=ANY(%(accounts)s)' + ('' if p['chat_ids'] is None else ' AND m.chat=ANY(%(chats)s)')


def _percentiles(delta):
    return _P.format(q=0.5, d=delta) + ' AS p50,' + _P.format(q=0.9, d=delta) + ' AS p90'


def _arrival():
    return '''SELECT 'p' AS row,m.account,'arrival' AS stage,e.origin,''' + CHAT_TYPE + ''' AS chat_type,
        ''' + MESSAGE_KIND + ''' AS message_kind,NULL AS policy,count(*) AS n,
        ''' + _percentiles('greatest(0,extract(epoch FROM e.created_at-m.sent_at))::float8') + '''
    FROM events e JOIN messages m ON m.id=e.message ''' + CHAT_JOINS + '''
    WHERE e.created_at>=%(cutoff)s AND e.kind='message.created' AND e.origin='live' AND {scope}
    GROUP BY 2,4,5,6'''


def _download(waiting=True):
    """The download percentiles and, unless the summary's job pass already read it (ANYM-917), what waits now."""
    measured = '''SELECT 'p' AS row,m.account,'download' AS stage,e.origin,''' + CHAT_TYPE + ''' AS chat_type,
        NULL AS message_kind,NULL AS policy,count(*) AS n,
        ''' + _percentiles('extract(epoch FROM e.created_at-created.at)::float8') + ''',
        NULL::bigint AS pending_n,NULL::float8 AS oldest_s,NULL::bigint AS on_request_n,NULL::bigint AS unknown_n
    FROM events e JOIN messages m ON m.id=e.message ''' + CHAT_JOINS + '''
    CROSS JOIN LATERAL (SELECT min(x.created_at) AS at FROM events x
        WHERE x.message=e.message AND x.kind='message.created') created
    WHERE e.created_at>=%(cutoff)s AND e.kind='attachment.ready' AND created.at IS NOT NULL AND {scope}
    GROUP BY 2,4,5'''
    return measured + '''
    UNION ALL
    SELECT 'now',m.account,'download',NULL,NULL,NULL,NULL,NULL,NULL,NULL,
        count(*) FILTER (WHERE el.claim='eligible'),
        extract(epoch FROM %(now)s-min(j.created_at) FILTER (WHERE el.claim='eligible'))::float8,
        count(*) FILTER (WHERE el.claim IN ''' + _ON_REQUEST + '''),
        count(*) FILTER (WHERE el.claim IN ''' + _UNKNOWN + ''')
    FROM media_jobs j JOIN messages m ON m.id=j.message AND m.account=j.account
    JOIN chats ch ON ch.id=m.chat AND ch.account=m.account ''' + ELIGIBILITY_JOIN + '''
    LEFT JOIN attachments a ON a.id=j.id AND a.message=j.message AND a.active
    WHERE j.state IN ('queued','leased') AND a.id IS NULL AND NOT m.deleted AND {scope}
    GROUP BY 2''' if waiting else measured


def _download_waiting(claims, now):
    """_download's `now` rows from sync_status' job rows with a claim (same jobs, claims and counts)."""
    rows = []
    for account in sorted(claims):
        n = {'eligible': 0, 'on_request': 0, 'unknown': 0}
        oldest = None
        for row in claims[account]:
            claim = row['eligibility']
            if claim == 'eligible':
                n['eligible'] += row['count']
                oldest = row['since'] if oldest is None else min(oldest, row['since'])
            elif claim in ON_REQUEST_CLAIMS:
                n['on_request'] += row['count']
            elif claim in UNKNOWN_CLAIMS:
                n['unknown'] += row['count']
        rows.append({'row': 'now', 'account': account, 'stage': 'download', 'pending_n': n['eligible'],
                     'oldest_s': None if oldest is None else (now - oldest).total_seconds(),
                     'on_request_n': n['on_request'], 'unknown_n': n['unknown']})
    return rows


def _files(withheld):
    # ANYM-917: the withheld rule reads the on-request chat set once per statement (a MATERIALIZED CTE, as
    # coverage.exclusion_sets does), not once per row and FILTER (prod 11-09: three SubPlans, 45 ms each).
    if withheld != 'false':
        withheld = ('(m.chat IN (SELECT orq.chat FROM on_request_chats orq) AND EXISTS(SELECT FROM media_jobs rj'
                    ' WHERE rj.id=a.id AND rj.request_key_digest IS NULL))')
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
    return ('WITH ' + on_request_set(withheld != 'false') + '''
    SELECT 'p' AS row,m.account,''' + stage + ''' AS stage,e.origin,''' + CHAT_TYPE + ''' AS chat_type,
        NULL AS message_kind,NULL AS policy,count(*) AS n,
        ''' + _percentiles('extract(epoch FROM e.created_at-ready.at)::float8') + ''',
        NULL::bigint AS pending_n,NULL::float8 AS oldest_s,NULL::bigint AS on_request_n
    FROM events e JOIN messages m ON m.id=e.message ''' + CHAT_JOINS + '''
    CROSS JOIN LATERAL (SELECT max(r.created_at) AS at FROM events r WHERE r.message=e.message
        AND r.kind='attachment.ready' AND r.created_at<=e.created_at
        AND r.snapshot->'processing'->>'attachment_id'=e.snapshot->'processing'->>'attachment_id') ready
    WHERE e.created_at>=%(cutoff)s
        AND e.kind IN ('file.text.ready','file.transcript.ready','file.description.ready','file.processing.failed')
        AND (e.kind='file.processing.failed' OR ready.at IS NOT NULL) AND {scope}
    GROUP BY 2,3,4,5
    UNION ALL ''' + waiting.replace('{stage}', "'text'").replace('{table}', 'attachment_text') + '''
    UNION ALL ''' + waiting.replace('{stage}', "CASE q.kind WHEN 'audio' THEN 'transcript' ELSE 'description' END")
        .replace('{table}', 'attachment_enrichment'))


def _subscriptions(owner):
    """match, hint and the hint wait over the owner's subscriptions (every one for the operator), each reference
    under the caller's current archive scope (a narrowed key never sees a chat it lost), never a masked
    (access_changed) definition."""
    who = 's.principal=%(principal)s AND s.key_id=%(key_id)s' if owner else 'true'
    return '''WITH own AS (SELECT s.id,s.state,coalesce(s.start_position,0) AS start_position,
            s.hints_deleted_through AS pruned,s.hints_deleted_at,
            d.subscription IS NOT NULL AND (d.receiver IS NOT NULL OR EXISTS(SELECT FROM delivery_endpoints de
                WHERE de.id=d.endpoint AND de.state='enabled')) AS expects_hints,
            CASE WHEN ''' + HELD_POLICY + ''' THEN 'held_by_policy' ELSE 'immediate' END AS policy
        FROM subscriptions s LEFT JOIN subscription_delivery d ON d.subscription=s.id
        WHERE ''' + who + ''' AND s.state<>'access_changed'),
    refs AS (SELECT own.id AS subscription,own.policy,own.pruned,own.hints_deleted_at,sm.event,sm.matched_at,
            m.account,e.origin,e.created_at
        FROM own JOIN subscription_matches sm ON sm.subscription=own.id JOIN events e ON e.id=sm.event
        JOIN messages m ON m.id=e.message
        WHERE NOT sm.hidden AND {scope}),
    recent AS (SELECT * FROM refs WHERE matched_at>=%(cutoff)s),
    -- The first delivered hint covering a match: the earliest finish among delivered events hints at or above its
    -- position, as one ordered pass per subscription. A match at or below the retention frontier, or matched before
    -- the last deletion minus the retention days, may have lost that hint: unknown, so no hint sample (ANYM-734).
    merged AS (SELECT o.subscription,o.through_position AS pos,1 AS k,o.finished_at,NULL::timestamptz AS matched_at,
            NULL AS account,NULL AS origin,NULL AS policy,false AS known
        FROM own JOIN subscription_outbox o ON o.subscription=own.id
        WHERE o.kind='events' AND o.state='delivered' AND o.finished_at IS NOT NULL
        UNION ALL
        SELECT subscription,event,0,NULL,matched_at,account,origin,policy,
            event>pruned AND (hints_deleted_at IS NULL OR matched_at>=hints_deleted_at-%(retention)s)
        FROM recent),
    covered AS (SELECT k,known,matched_at,account,origin,policy,
            min(finished_at) OVER (PARTITION BY subscription ORDER BY pos DESC,k DESC ROWS UNBOUNDED PRECEDING) AS finished
        FROM merged),
    waiting AS (SELECT r.matched_at,r.account,
            coalesce(w.held_reason IN ('debounce','batch','quiet_hours') AND w.due_at>%(now)s
                AND r.event>coalesce(w.hinted_through,own.start_position),false) AS held
        FROM own LEFT JOIN subscription_hint_windows w ON w.subscription=own.id
        CROSS JOIN LATERAL (SELECT coalesce(max(o.through_position),0) AS through FROM subscription_outbox o
            WHERE o.subscription=own.id AND o.kind='events' AND o.state IN ''' + HINT_FINAL + ''') final
        JOIN refs r ON r.subscription=own.id AND r.event>greatest(final.through,own.start_position,own.pruned)
        WHERE own.expects_hints AND own.state='active')
    SELECT 'p' AS row,account,'match' AS stage,origin,NULL AS chat_type,NULL AS message_kind,NULL AS policy,
        count(*) AS n,''' + _percentiles('extract(epoch FROM matched_at-created_at)::float8') + ''',
        NULL::bigint AS pending_n,NULL::float8 AS oldest_s,NULL::bigint AS held_n
    FROM recent GROUP BY 2,4
    UNION ALL
    SELECT 'p',account,'hint',origin,NULL,NULL,policy,count(*),
        ''' + _percentiles('extract(epoch FROM finished-matched_at)::float8') + ''',NULL,NULL,NULL
    FROM covered WHERE k=0 AND known AND finished IS NOT NULL GROUP BY 2,4,7
    UNION ALL
    SELECT 'now',account,'hint',NULL,NULL,NULL,NULL,NULL,NULL,NULL,count(*) FILTER (WHERE NOT held),
        extract(epoch FROM %(now)s-min(matched_at) FILTER (WHERE NOT held))::float8,count(*) FILTER (WHERE held)
    FROM waiting GROUP BY 2'''


def _bounded(c, timeout_s, run):
    """run() under its own savepoint, statement timeout and jit=off; the caller's timeout is restored after."""
    with c.transaction():
        previous = c.execute("SELECT current_setting('statement_timeout') AS v").fetchone()['v']
        c.execute("SELECT set_config('statement_timeout',%s,true),set_config('jit','off',true)",
                  (f'{max(1, int(timeout_s * 1000))}ms',))
        result = run()
        c.execute("SELECT set_config('statement_timeout',%s,true)", (previous,))
    return result


def read(c, p, window_h=DEFAULT_WINDOW, owner=None, claims=None):
    """{latency, latency_window_h, latency_pending, latency_unavailable} for scope p (accounts, chat_ids).

    owner=(principal, key_id) limits match and hint rows to that key's own subscriptions; None (the operator's
    table) reads every subscription. claims: sync_status.get_sync_status' job rows of this unscoped p (ANYM-917):
    the download group then reads only its percentiles and takes what waits now from those rows.
    """
    if not valid_window(window_h):
        raise ValueError('invalid_latency_window')
    deadline = time.monotonic() + TIMEOUTS[window_h]
    result = {'latency': [], 'latency_window_h': window_h, 'latency_pending': [], 'latency_unavailable': []}

    def start():
        return c.execute('SELECT clock_timestamp() AS t').fetchone()['t'], recognition_withheld(c)

    try:
        now, withheld = _bounded(c, TIMEOUTS[window_h], start)
    except psycopg.Error as error:
        log.warning('stage_latency_unavailable group=start exception_type=%s', type(error).__name__)
        result['latency_unavailable'] = list(STAGES)
        return result
    args = {'accounts': list(p['accounts']), 'chats': p['chat_ids'], 'cutoff': now - timedelta(hours=window_h),
            'now': now, 'retention': timedelta(days=CLOSED_RETENTION_DAYS),
            'principal': owner[0] if owner else None, 'key_id': owner[1] if owner else None}
    scope = _scope(p)
    # Fragments such as ELIGIBILITY_JOIN hold regex braces: placeholders are replaced, never str.format'ed.
    groups = (('arrival', ('arrival',), _arrival().replace('{scope}', scope)),
              ('download', ('download',), _download(claims is None).replace('{scope}', scope)),
              ('files', ('text', 'transcript', 'description', 'failed'), _files(withheld).replace('{scope}', scope)),
              ('subscriptions', ('match', 'hint'), _subscriptions(owner).replace('{scope}', scope)))
    for name, stages, sql in groups:
        left = deadline - time.monotonic()
        if left < 0.05:
            log.warning('stage_latency_unavailable group=%s exception_type=budget_spent', name)
            result['latency_unavailable'].extend(stages)
            continue
        try:
            rows = _bounded(c, left, lambda: c.execute(sql, args).fetchall())
        except psycopg.Error as error:
            # A timeout or any database error degrades only this group's stages (criterion 4).
            log.warning('stage_latency_unavailable group=%s exception_type=%s', name, type(error).__name__)
            result['latency_unavailable'].extend(stages)
            continue
        if name == 'download' and claims is not None:
            rows = rows + _download_waiting(claims, now)
        for row in rows:
            if row['row'] == 'p':
                failed = row['stage'] == 'failed'
                result['latency'].append({'account': row['account'], 'stage': row['stage'], 'origin': row['origin'],
                    'chat_type': row['chat_type'], 'message_kind': row['message_kind'], 'policy': row['policy'],
                    'n': row['n'], 'p50_s': None if failed else _seconds(row['p50']),
                    'p90_s': None if failed else _seconds(row['p90']), 'window_h': window_h})
                continue
            extra = {k: row[k] or 0 for k in ('held_n', 'on_request_n', 'unknown_n') if k in row}
            if not row['pending_n'] and not any(extra.values()):
                continue
            result['latency_pending'].append({'account': row['account'], 'stage': row['stage'],
                'pending': {'n': row['pending_n'], 'oldest_s': _seconds(row['oldest_s'])}, **extra})
    order = {stage: n for n, stage in enumerate(STAGES)}
    result['latency'].sort(key=lambda r: (r['account'], order[r['stage']], *(r[k] or '' for k in
        ('origin', 'chat_type', 'message_kind', 'policy'))))
    result['latency_pending'].sort(key=lambda r: (r['account'], order[r['stage']]))
    return result


def table(result):
    """The operator's one table: percentile rows, then what waits now (blank where a column does not apply)."""
    head = ('account', 'stage', 'origin', 'chat_type', 'message_kind', 'policy', 'n', 'p50_s', 'p90_s',
            'pending_n', 'oldest_s', 'on_request_n', 'unknown_n', 'held_n')
    lines: list[tuple] = [head]
    for row in result['latency']:
        lines.append(tuple(row[k] for k in head[:9]) + (None,) * 5)
    for row in result['latency_pending']:
        lines.append((row['account'], row['stage']) + (None,) * 7 + (row['pending']['n'], row['pending']['oldest_s'],
                     row.get('on_request_n'), row.get('unknown_n'), row.get('held_n')))
    cells = [['' if v is None else str(v) for v in line] for line in lines]
    widths = [max(len(line[i]) for line in cells) for i in range(len(head))]
    text = ['  '.join(v.ljust(w) for v, w in zip(line, widths)).rstrip() for line in cells]
    text.append(f"window_h={result['latency_window_h']}"
                + (' unavailable=' + ','.join(result['latency_unavailable']) if result['latency_unavailable'] else ''))
    return '\n'.join(text)
