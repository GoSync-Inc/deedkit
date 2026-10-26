"""Messenger deletion purge (ANYM-511).

A message deleted in the messenger loses its text, file names, file text, transcripts and AI output
within minutes; agents keep a content-free mark. The worker runs under its own role
(purge_role.sql): each transaction clears at most 50 messages under `archive:<account>` and
`purge:<account>`, both taken without waiting, with a 2 s lock and statement bound and a 3 s
transaction bound, so ingest, sends and commands waiting behind it wait at most 3 s. It gives way
on contention; a failure stops only the purge, is retried with backoff (one message at a time
after a failure) and is reported through get_sync_status. Logs carry counts only.
"""
import argparse
from contextlib import contextmanager
import json
import logging
from pathlib import Path
import sys
import time

import psycopg
from psycopg import sql

log=logging.getLogger('messaging_service.message_purge')
BATCH=50
APPLY_BATCH=500
APPLY_RETRIES=3
PASS_SECONDS=10
EVENT_WINDOW=5000
LINK_CHUNK=5000
OVERDUE_SECONDS=900
AUDIT_IDS=100
PURGE_STATEMENT='2s'
PURGE_TRANSACTION='3s'
HIDE_STATEMENT='500ms'
# The listing hides a chat whose every message is purged; it reads only this mark.
HIDE_CHATS='''UPDATE chats ch SET purged_at=clock_timestamp() WHERE ch.account=%s
    AND ch.id IN (SELECT chat FROM messages WHERE id=ANY(%s)) AND NOT EXISTS(SELECT 1 FROM messages m
        WHERE m.account=ch.account AND m.chat=ch.id AND NOT m.purged)'''
REASONS=('deleted_for_everyone','deleted_for_me','chat_deleted','chat_cleared','telegram_deleted','existing_deleted')
GUARANTEE={'telegram':'best_effort','whatsapp':'reported'}
# The provider chat of a message: its chat row, else its newest revision (017 left chats.provider_chat nullable).
PROVIDER_CHAT='''coalesce(nullif(ch.provider_chat,''),(SELECT r.snapshot->>'provider_chat' FROM message_revisions r
    WHERE r.message=m.id ORDER BY r.revision DESC LIMIT 1),'')'''


def lock_key(name):
    return sql.SQL('hashtextextended({},0)').format(sql.Literal(name))


class Purger:
    def __init__(self,store,role=None):
        self.store=store;self.role=role

    @contextmanager
    def tx(self,statement_timeout='10s',transaction_timeout=None):
        with self.store.pool.connection() as c:
            if self.role:c.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(self.role)))
            if transaction_timeout:
                c.execute(sql.SQL('SET LOCAL transaction_timeout={}').format(sql.Literal(transaction_timeout)))
            c.execute("SET LOCAL lock_timeout='2s'")
            c.execute(sql.SQL('SET LOCAL statement_timeout={}').format(sql.Literal(statement_timeout)))
            yield c

    @staticmethod
    def state(c,name):
        return c.execute('SELECT value FROM purge_state WHERE name=%s',(name,)).fetchone()['value']

    @staticmethod
    def set_state(c,name,value):
        c.execute('UPDATE purge_state SET value=%s WHERE name=%s',(value,name))

    def enabled(self):
        with self.tx() as c:return self.state(c,'enabled')=='true'

    def set_enabled(self,on):
        with self.tx() as c:self.set_state(c,'enabled','true' if on else 'false')
        return {'purge':'enabled' if on else 'disabled'}

    def discover(self):
        """Queue every message.deleted event after the cursor, one bounded window per call."""
        with self.tx() as c:
            cursor=int(self.state(c,'event_cursor'))
            head=c.execute('SELECT coalesce(max(id),0) AS head FROM events').fetchone()['head']
            upper=min(head,cursor+EVENT_WINDOW)
            if upper<=cursor:return 0
            rows=c.execute('''INSERT INTO message_purges(message,account,provider_chat,provider_id,reason,observed_at)
                SELECT DISTINCT ON(m.id) m.id,m.account,'''+PROVIDER_CHAT+''',m.provider_id,
                    CASE a.platform WHEN 'telegram' THEN 'telegram_deleted' ELSE 'deleted_for_everyone' END,e.created_at
                FROM events e JOIN messages m ON m.id=e.message JOIN chats ch ON ch.id=m.chat
                JOIN accounts a ON a.id=m.account
                WHERE e.id>%s AND e.id<=%s AND e.kind='message.deleted' AND m.deleted AND NOT m.purged
                ORDER BY m.id,e.id ON CONFLICT(message) DO NOTHING RETURNING message''',(cursor,upper)).fetchall()
            self.set_state(c,'event_cursor',str(upper))
            return len(rows)

    def backfill_links(self):
        """Link receipts stored before schema 45, one chunk per call; True once every receipt is linked.

        No purge completes before this returns True (purge_account checks it in its transaction): an
        unlinked receipt would keep a purged message's text past the deadline. The guard trigger links
        every receipt stored after the migration. Concurrent callers (worker, apply) take turns on
        `purge-backfill` without waiting."""
        with self.tx() as c:
            if self.state(c,'receipt_backfill')=='complete':return True
            if not c.execute(sql.SQL('SELECT pg_try_advisory_xact_lock({}) AS ok').format(
                    lock_key('purge-backfill'))).fetchone()['ok']:return False
            cursor=self.state(c,'receipt_cursor')
            row=c.execute('''WITH chunk AS (SELECT digest,account,coalesce(envelope->>'chat','') AS provider_chat,
                    coalesce(envelope->>'provider_id','') AS provider_id FROM ingest_receipts
                    WHERE digest>%s ORDER BY digest LIMIT %s),
                linked AS (INSERT INTO ingest_receipt_links SELECT * FROM chunk ON CONFLICT(digest) DO NOTHING)
                SELECT max(digest) AS last,count(*) AS n FROM chunk''',(cursor,LINK_CHUNK)).fetchone()
            if row['n']<LINK_CHUNK:
                self.set_state(c,'receipt_backfill','complete');return True
            self.set_state(c,'receipt_cursor',row['last']);return False

    def backfill(self,seconds=PASS_SECONDS):
        """Link chunks until every receipt is linked (True) or `seconds` pass (False)."""
        started=time.monotonic()
        while not self.backfill_links():
            if time.monotonic()-started>=seconds:return False
        return True

    def pending_accounts(self):
        with self.tx() as c:
            return [r['account'] for r in c.execute('''SELECT DISTINCT account FROM message_purges
                WHERE purged_at IS NULL AND next_attempt_at<=clock_timestamp() ORDER BY account''')]

    def pending_count(self):
        with self.tx() as c:
            return c.execute('SELECT count(*) AS n FROM message_purges WHERE purged_at IS NULL').fetchone()['n']

    def purge_account(self,account,limit=BATCH):
        """One pass: at most `limit` pending messages of one account in one bounded transaction."""
        ids=[]
        try:
            with self.tx(PURGE_STATEMENT,PURGE_TRANSACTION) as c:
                for name in ('purge:'+account,'archive:'+account):
                    if not c.execute(sql.SQL('SELECT pg_try_advisory_xact_lock({}) AS ok').format(lock_key(name))
                                     ).fetchone()['ok']:
                        return {'purged':0,'gave_way':1,'failed':0}
                # Until every older receipt is linked a purge could miss one: the mark stays pending
                # (and turns overdue) instead of reporting a purge that left a copy.
                if self.state(c,'receipt_backfill')!='complete':return {'purged':0,'gave_way':0,'failed':0}
                rows=c.execute('''SELECT message,provider_chat,provider_id,attempts FROM message_purges
                    WHERE account=%s AND purged_at IS NULL AND next_attempt_at<=clock_timestamp()
                    ORDER BY next_attempt_at,message LIMIT %s FOR UPDATE SKIP LOCKED''',(account,limit)).fetchall()
                if not rows:return {'purged':0,'gave_way':0,'failed':0}
                # After a failure, one message at a time: a slow batch shrinks until it fits the bound.
                retried=[r for r in rows if r['attempts']]
                if retried:rows=retried[:1]
                ids=[r['message'] for r in rows]
                counts=self.scrub(c,account,rows)
                c.execute('''UPDATE message_purges SET purged_at=clock_timestamp(),counts=%s,failure_code=NULL
                    WHERE message=ANY(%s)''',(json.dumps(counts),ids))
                for start in range(0,len(ids),AUDIT_IDS):
                    c.execute('''INSERT INTO audit_log(actor,operation,outcome,resource_ids)
                        VALUES('operator','message_purge','ok',%s)''',(ids[start:start+AUDIT_IDS],))
            log.info('message_purge purged=%d',len(ids))
            return {'purged':len(ids),'gave_way':0,'failed':0}
        except psycopg.Error as exc:
            code=('lock_timeout' if isinstance(exc,psycopg.errors.LockNotAvailable) else
                  'statement_timeout' if isinstance(exc,psycopg.errors.QueryCanceled) else
                  # The server ends the session at the transaction bound; the pool drops that connection,
                  # and a client that notices only the closed socket sees no SQLSTATE.
                  'transaction_timeout' if isinstance(exc,psycopg.errors.TransactionTimeout) or (
                      isinstance(exc,psycopg.OperationalError) and exc.sqlstate is None) else 'purge_error')
            self.failed(account,ids,code)
            log.warning('message_purge failed=%d code=%s',len(ids),code)
            return {'purged':0,'gave_way':int(code!='purge_error'),'failed':len(ids)}

    def failed(self,account,ids,code):
        with self.tx() as c:
            c.execute('''UPDATE message_purges SET attempts=attempts+1,failure_code=%s,
                next_attempt_at=clock_timestamp()+least(interval '1 hour',interval '5 seconds'*power(2,least(attempts,10)))
                WHERE account=%s AND purged_at IS NULL AND (message=ANY(%s) OR cardinality(%s::text[])=0)''',
                (code,account,ids,ids))

    @staticmethod
    def scrub(c,account,rows):
        ids=[r['message'] for r in rows]
        chats=[r['provider_chat'] for r in rows];provider_ids=[r['provider_id'] for r in rows]
        def n(statement,args):return c.execute(statement,args).rowcount
        c.execute('SELECT id FROM messages WHERE id=ANY(%s) AND account=%s FOR UPDATE',(ids,account))
        # The flag first: after commit every guard trigger treats a late write as purged.
        counts={'messages':n("UPDATE messages SET body='',purged=true WHERE id=ANY(%s) AND account=%s",(ids,account))}
        files=[r['id'] for r in c.execute('SELECT id FROM attachments WHERE message=ANY(%s)',(ids,))]
        counts['file_names']=n("UPDATE attachments SET filename='' WHERE message=ANY(%s) AND filename<>''",(ids,))
        derived=0
        for table in ('attachment_text','attachment_enrichment','attachment_understanding',
                      'attachment_text_chunks','attachment_enrichment_chunks'):
            derived+=n(sql.SQL('DELETE FROM {} WHERE attachment=ANY(%s)').format(sql.Identifier(table)),(files,))
        counts['derived']=derived
        counts['grants']=n('DELETE FROM download_grants WHERE attachment=ANY(%s)',(files,))
        counts['media_jobs']=n('''UPDATE media_jobs SET descriptor=descriptor-'filename',
            state=CASE WHEN state IN ('queued','leased') THEN 'superseded' ELSE state END,
            lease_owner=NULL,lease_until=NULL,lease_epoch=lease_epoch+1,requested_until=NULL,request_key_digest=NULL
            WHERE message=ANY(%s)''',(ids,))
        counts['revisions']=n('UPDATE message_revisions SET snapshot=public.purged_envelope(snapshot) WHERE message=ANY(%s)',(ids,))
        counts['events']=n('UPDATE events SET snapshot=public.purged_envelope(snapshot) WHERE message=ANY(%s)',(ids,))
        counts['receipts']=n('''UPDATE ingest_receipts r SET envelope=public.purged_envelope(r.envelope)
            FROM ingest_receipt_links l JOIN unnest(%s::text[],%s::text[]) AS t(provider_chat,provider_id)
                ON t.provider_chat=l.provider_chat AND t.provider_id=l.provider_id
            WHERE l.account=%s AND r.digest=l.digest''',(chats,provider_ids,account))
        counts['metadata']=n('DELETE FROM message_metadata WHERE message=ANY(%s)',(ids,))
        # Review results lose their reference text (an exception to ANYM-20's append-only rows).
        counts['reviews']=n("UPDATE reviews SET result_ref='[deleted]' WHERE message=ANY(%s) AND result_ref<>'[deleted]'",(ids,))
        # A send is this message by its verified wire reference (native WhatsApp may archive a LID chat
        # for a PN request); a send without one by its requested chat and returned ID.
        counts['sends']=n('''UPDATE outgoing_messages o SET body='[deleted]',
            source_text=CASE WHEN o.source_text IS NULL THEN NULL ELSE '[deleted]' END,
            render_artifact=CASE WHEN o.render_artifact IS NULL THEN NULL ELSE '{}'::jsonb END
            WHERE o.account=%s AND EXISTS(SELECT 1 FROM unnest(%s::text[],%s::text[]) AS t(provider_chat,provider_id)
                WHERE CASE WHEN o.archive_reference IS NULL
                    THEN o.provider_chat=t.provider_chat AND o.provider_message_id=t.provider_id
                    ELSE o.archive_reference->>'provider_chat_id'=t.provider_chat
                        AND o.archive_reference->>'provider_message_id'=t.provider_id END)''',
            (account,chats,provider_ids))
        # Commands that target it, or produced it (send_poll, send_file ...), keep their request IDs,
        # status, routing and authorization binding; only their text goes.
        counts['commands']=n('''UPDATE messenger_commands mc SET payload=public.purged_command_payload(mc.payload),
            result=public.purged_command_result(mc.result)
            WHERE mc.account=%s AND (mc.message=ANY(%s) OR (mc.message IS NULL AND mc.result IS NOT NULL AND EXISTS(
                SELECT 1 FROM unnest(%s::text[],%s::text[]) AS t(provider_chat,provider_id)
                WHERE (t.provider_chat=mc.payload->>'provider_chat' AND t.provider_id=mc.result->>'provider_message_id')
                    OR (t.provider_chat=mc.result#>>'{data,archive_reference,provider_chat_id}'
                        AND t.provider_id=mc.result#>>'{data,archive_reference,provider_message_id}'))))''',
            (account,ids,chats,provider_ids))
        hits=[json.dumps([{'chat':m['chat'],'provider_id':m['provider_id']}]) for m in
              c.execute('SELECT chat,provider_id FROM messages WHERE id=ANY(%s)',(ids,))]
        counts['live_results']=n('''UPDATE messenger_commands SET result=public.purged_command_hits(account,result)
            WHERE account=%s AND jsonb_typeof(result#>'{data,hits}')='array' AND result#>'{data,hits}' @> ANY(%s::jsonb[])''',
            (account,hits))
        counts['reply_notes']=sum(n(sql.SQL('''UPDATE {} SET note=NULL WHERE note IS NOT NULL
            AND (message=ANY(%s) OR incoming_head=ANY(%s))''').format(sql.Identifier(t)),(ids,ids))
            for t in ('reply_dismissals','reply_snoozes'))
        # Last, in a savepoint with its own short bound: proving that a chat has no unpurged message
        # left reads its purged history (message_chat_date index), which in a long chat may not fit.
        # Then the content purge still commits and only the chat stays listed (hidden_chats_skipped).
        try:
            with c.transaction():
                c.execute(sql.SQL('SET LOCAL statement_timeout={}').format(sql.Literal(HIDE_STATEMENT)))
                counts['hidden_chats']=n(HIDE_CHATS,(account,ids))
        except psycopg.errors.QueryCanceled:
            counts['hidden_chats']=0;counts['hidden_chats_skipped']=1
        # A released savepoint keeps its SET LOCAL until the transaction ends.
        c.execute(sql.SQL('SET LOCAL statement_timeout={}').format(sql.Literal(PURGE_STATEMENT)))
        return counts

    def run_once(self):
        totals={'discovered':0,'purged':0,'gave_way':0,'failed':0,'links_complete':False}
        if not self.enabled():return {**totals,'enabled':False}
        totals['discovered']=self.discover()
        # Purges wait until every older receipt is linked; until then their marks stay pending.
        totals['links_complete']=self.backfill()
        if not totals['links_complete']:return {**totals,'enabled':True}
        for account in self.pending_accounts():
            started=time.monotonic()
            # Drain the account while it makes progress, within one pass: after a failure the
            # batches are single messages, and one per pass would leave a backlog for minutes.
            while True:
                result=self.purge_account(account)
                for key,value in result.items():totals[key]+=value
                if not result['purged'] or time.monotonic()-started>=PASS_SECONDS:break
        return {**totals,'enabled':True}

    def preview(self):
        """Read-only counts of messages already marked deleted and not purged, by account."""
        with self.tx('120s') as c:
            c.execute('SET TRANSACTION READ ONLY')
            return [dict(r) for r in c.execute('''SELECT m.account,count(DISTINCT m.id) AS messages,
                count(a.id) AS originals,coalesce(sum(a.bytes),0)::bigint AS bytes
                FROM messages m LEFT JOIN attachments a ON a.message=m.id
                WHERE m.deleted AND NOT m.purged GROUP BY m.account ORDER BY m.account''')]

    def apply(self):
        """Purge messages already marked deleted (reason existing_deleted), queued in passes of at most
        500, and drain every due pending mark, including marks an interrupted run left queued."""
        while not self.backfill():time.sleep(0.05)
        totals={'queued':0,'purged':0,'gave_way':0,'failed':0,'passes':0}
        retries=0
        while True:
            with self.tx('120s') as c:
                rows=c.execute('''INSERT INTO message_purges(message,account,provider_chat,provider_id,reason,observed_at)
                    SELECT m.id,m.account,'''+PROVIDER_CHAT+''',m.provider_id,'existing_deleted',
                        coalesce(m.provider_updated_at,m.sent_at)
                    FROM messages m JOIN chats ch ON ch.id=m.chat
                    WHERE m.deleted AND NOT m.purged AND NOT EXISTS(SELECT 1 FROM message_purges p WHERE p.message=m.id)
                    ORDER BY m.id LIMIT %s ON CONFLICT(message) DO NOTHING RETURNING message''',
                    (APPLY_BATCH,)).fetchall()
            totals['queued']+=len(rows)
            progress=gave_way=0
            for account in self.pending_accounts():
                while True:
                    result=self.purge_account(account)
                    for key in ('purged','gave_way','failed'):totals[key]+=result[key]
                    progress+=result['purged'];gave_way+=result['gave_way']
                    if not result['purged']:break
            if rows or progress:totals['passes']+=1
            if rows or progress:continue
            # Only contention left (the live worker holds the account): wait briefly, then report.
            if gave_way and retries<APPLY_RETRIES:
                retries+=1;time.sleep(1);continue
            break
        totals['pending']=self.pending_count()
        totals['complete']=totals['pending']==0
        log.info('message_purge apply queued=%d purged=%d gave_way=%d failed=%d pending=%d',totals['queued'],
                 totals['purged'],totals['gave_way'],totals['failed'],totals['pending'])
        return totals

    def status(self):
        with self.tx() as c:
            return {'failed':[dict(r) for r in c.execute('''SELECT message,account,attempts,
                CASE WHEN observed_at<clock_timestamp()-make_interval(secs=>%s) THEN 'purge_overdue' ELSE failure_code END AS code
                FROM message_purges WHERE purged_at IS NULL AND (failure_code IS NOT NULL
                    OR observed_at<clock_timestamp()-make_interval(secs=>%s)) ORDER BY account,message''',
                (OVERDUE_SECONDS,OVERDUE_SECONDS))],
                    'accounts':deletions(c,[r['id'] for r in c.execute('SELECT id FROM accounts ORDER BY id')])}


def deletions(c,accounts):
    """The `deletions` block of get_sync_status per account; content-free."""
    from .envelope import stable_id
    if not accounts:return {}
    if not c.execute("SELECT to_regclass('public.message_purges') IS NOT NULL AND "
                     "has_table_privilege('public.message_purges','SELECT') AND "
                     "has_table_privilege('public.chat_tombstones','SELECT') AS ok").fetchone()['ok']:
        return {}
    platforms={r['id']:r['platform'] for r in c.execute('SELECT id,platform FROM accounts WHERE id=ANY(%s)',(accounts,))}
    totals={r['account']:r for r in c.execute('''SELECT account,count(*) FILTER(WHERE purged_at IS NOT NULL) AS purged_total,
        count(*) FILTER(WHERE purged_at IS NULL) AS pending,max(purged_at) AS last_purge_at
        FROM message_purges WHERE account=ANY(%s) GROUP BY account''',(accounts,))}
    failed={}
    for r in c.execute('''SELECT account,CASE WHEN observed_at<clock_timestamp()-make_interval(secs=>%s)
            THEN 'purge_overdue' ELSE failure_code END AS code,count(*) AS n
        FROM message_purges WHERE account=ANY(%s) AND purged_at IS NULL AND (failure_code IS NOT NULL
            OR observed_at<clock_timestamp()-make_interval(secs=>%s)) GROUP BY 1,2 ORDER BY 1,2''',
            (OVERDUE_SECONDS,accounts,OVERDUE_SECONDS)):
        item=failed.setdefault(r['account'],{'count':0,'codes':{}})
        item['count']+=r['n'];item['codes'][r['code']]=r['n']
    chats={}
    for r in c.execute('''SELECT t.account,t.provider_chat,min(t.due_at) AS due_at,
            (SELECT count(*) FROM messages m JOIN chats ch ON ch.id=m.chat WHERE ch.account=t.account
             AND ch.provider_chat=t.provider_chat AND NOT m.purged) AS messages
        FROM chat_tombstones t WHERE t.account=ANY(%s) AND t.state='pending'
        GROUP BY t.account,t.provider_chat ORDER BY 1,3''',(accounts,)):
        chats.setdefault(r['account'],[]).append({'chat':stable_id('chat',r['account'],r['provider_chat']),
            'messages':r['messages'],'due_at':r['due_at']})
    result={}
    for account in accounts:
        row=totals.get(account,{})
        result[account]={'guarantee':GUARANTEE.get(platforms.get(account,''),'best_effort'),'gaps':[],
            'purged_total':row.get('purged_total',0),'pending':row.get('pending',0),
            'pending_chat_purges':chats.get(account,[]),'failed':failed.get(account,{'count':0,'codes':{}}),
            'last_purge_at':row.get('last_purge_at')}
    return result


def main(argv=None):
    from .store import Store
    parser=argparse.ArgumentParser(prog='python -m messaging_service.message_purge')
    parser.add_argument('--dsn-file',required=True)
    parser.add_argument('--files',default='/data/files')
    parser.add_argument('--role',help='SET ROLE for every transaction, e.g. messaging_purge')
    sub=parser.add_subparsers(dest='command',required=True)
    run=sub.add_parser('run');run.add_argument('--once',action='store_true');run.add_argument('--interval',type=float,default=15)
    deleted=sub.add_parser('purge-deleted');mode=deleted.add_mutually_exclusive_group(required=True)
    mode.add_argument('--preview',action='store_true');mode.add_argument('--apply',action='store_true')
    sub.add_parser('purge-status')
    sub.add_parser('purge-enable');sub.add_parser('purge-disable')
    args=parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,format='%(levelname)s %(name)s %(message)s')
    store=Store(Path(args.dsn_file).read_text().strip(),Path(args.files),'http://localhost')
    purger=Purger(store,args.role)
    def out(value):print(json.dumps(value,default=str,indent=2,sort_keys=True))
    try:
        if args.command=='run':
            while True:
                try:result=purger.run_once()
                # Any failure ends only this pass; the class name alone reaches the log.
                except Exception as exc:
                    log.warning('message_purge pass_failed %s',type(exc).__name__);result=None
                if args.once:out(result);return 0 if result is not None else 1
                time.sleep(args.interval)
        try:
            if args.command=='purge-deleted':
                if args.preview:out(purger.preview());return 0
                result=purger.apply();out(result)
                return 0 if result['complete'] else 1
            elif args.command=='purge-status':out(purger.status())
            else:out(purger.set_enabled(args.command=='purge-enable'))
        except psycopg.Error as exc:
            # The operator gets a code and runs the command again; nothing is half-applied.
            out({'error':type(exc).__name__});return 1
        return 0
    finally:
        store.close()


if __name__=='__main__':
    sys.exit(main())
