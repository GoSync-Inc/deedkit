"""PostgreSQL archive: one policy layer for HTTP and MCP.

No provider sessions, network fetch, arbitrary SQL or server approval gate is
exposed to agents. Reviewed opt-in operations use the existing fenced collectors.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone, timedelta  # noqa: F401  (timedelta moved with key admin)
import hashlib
import os
from pathlib import Path
import re
import secrets
import time
import json
import threading
from collections import OrderedDict
from uuid import UUID

import psycopg
from psycopg.errors import LockNotAvailable, QueryCanceled, TransactionTimeout
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from . import __version__  # noqa: F401  (still importable from here)
from .field_errors import date_range_refusal
from .policy import (  # noqa: F401  (key_admin reads these through this module)
                     DEFAULT_PERMISSIONS, OPERATION_PERMISSIONS, UNSET, audit,
                     key_id, lock_principal, normalize_policy, principal_id,
                     scope_filter, targets, effective_permissions, PERMISSIONS, normalize_list,
                     LEGACY_PERMISSIONS)
from .telemetry import source_sha
from .capabilities import CAPABILITY_SECTIONS, capability_view  # noqa: F401  (moved; still importable from here)


# When this messaging server process started (ANYM-592): with the source commit, "commit X running since T".
STARTED_AT=datetime.now(timezone.utc)


def build():
    """The commit this server was built from (telemetry's rule) and its start time, read anew on every call."""
    return {'source_sha':source_sha(),'started_at':STARTED_AT.strftime('%Y-%m-%dT%H:%M:%S.%fZ')}


class APIError(Exception):
    def __init__(self, code: str, status: int = 400):
        self.code, self.status = code, status
        super().__init__(code)


# Under the 5 s request budget, so a writer queued behind a migrate lock request still finishes.
MIGRATE_LOCK_TIMEOUT="SET LOCAL lock_timeout='2s'"


# migrate-expand (ANYM-596): the session budgets as SHOW prints them; all three below a request's 5 s.
EXPAND_SETTINGS={'lock_timeout':'2s','statement_timeout':'4s','transaction_timeout':'4s','search_path':'public'}


class ExpandError(Exception):
    """A fixed migrate-expand code; never PostgreSQL's message."""
    def __init__(self, code):
        self.code=code
        super().__init__(code)


# The database a connection reached: cluster system identifier, database name and OID (ANYM-449).
EXPAND_IDENTITY=("SELECT ARRAY[(SELECT system_identifier::text FROM pg_control_system()),current_database()::text,"
    "(SELECT oid::text FROM pg_database WHERE datname=current_database())] AS identity")


def expand_connection(dsn, application_name=None):
    """The only connection of migrate-expand (ANYM-596): its budgets, `public` only and no notices, read back.
    `application_name` overrides one the DSN names, so deploy finds this session by it (ANYM-449)."""
    c=psycopg.connect(dsn,row_factory=dict_row,prepare_threshold=None,connect_timeout=5,
        options='-c lock_timeout=2000 -c statement_timeout=4000 -c transaction_timeout=4000 -c search_path=public '
                '-c client_min_messages=error -c timezone=UTC',
        **({'application_name':application_name} if application_name else {}))
    try:
        if [c.execute(f'SHOW {name}').fetchone()[name] for name in EXPAND_SETTINGS]!=list(EXPAND_SETTINGS.values()):
            raise ExpandError('settings_refused')
        if application_name and c.execute('SHOW application_name').fetchone()['application_name']!=application_name:
            raise ExpandError('settings_refused')
    except BaseException:
        c.close(); raise
    return c


def expand_code(run):
    """`run()` with every failure as a fixed code. The connection is closed, never rolled back by psycopg's
    context manager, whose rollback failure logs PostgreSQL's message (Astra r1 F4); the server discards the
    open transaction when the session ends."""
    try:
        return run()
    except ExpandError:
        raise
    except LockNotAvailable:
        raise ExpandError('lock_timeout') from None
    except TransactionTimeout:
        raise ExpandError('transaction_timeout') from None
    except QueryCanceled:
        raise ExpandError('statement_timeout') from None
    except Exception:
        raise ExpandError('failed') from None


def expand_status(dsn, application_name=None):
    def run():
        c=expand_connection(dsn,application_name)
        try: return {'schema_version':ledger_version(c)}
        finally: c.close()
    return expand_code(run)


def migrate_expand(dsn, plan, *, root=None, before_commit=None, application_name=None):
    """Applies only DROP-free expand migrations and appended role grants in one bounded transaction on one
    connection, or nothing (ANYM-596). Never schema.sql, lock_replay_tables or the ACL repair of migrate().
    A plan `identity` (EXPAND_IDENTITY of the database deploy copied) must be what this connection reached before
    anything else runs (ANYM-449). Raises ExpandError with a fixed code; `before_commit(c)` is a test hook."""
    from . import migration_rollout as rollout
    root=Path(root) if root else Path(__file__).parent
    files={int(p.name[:3]):p for p in root.joinpath('migrations').glob('[0-9][0-9][0-9]_*.sql')}
    roles={p.name:p.read_bytes() for p in root.glob('*_role.sql')}
    try:
        base,target,prefixes,identity=plan['base'],plan['target'],plan['roles'],plan.get('identity')
        valid=((identity is None or type(identity) is list and len(identity)==3 and all(type(i) is str for i in identity))
            and all(type(v) is int for v in (base,target)) and base<=target==max(files) and type(prefixes) is dict
            and set(prefixes)==set(roles) and all(type(p) is dict and type(p.get('bytes')) is int
            and 0<=p['bytes']<=len(roles[n]) and type(p.get('sha256')) is str for n,p in prefixes.items()))
    except (KeyError,TypeError,ValueError):
        valid=False
    if not valid: raise ExpandError('plan_invalid')
    try:
        appended={}
        for name,prefix in sorted(prefixes.items()):
            data=roles[name]
            if hashlib.sha256(data[:prefix['bytes']]).hexdigest()!=prefix['sha256']: raise ExpandError('prefix_mismatch')
            appended[name]=(data[:prefix['bytes']].decode(),data.decode())
    except UnicodeDecodeError:
        raise ExpandError('prefix_mismatch') from None
    pending=[(v,files[v].read_bytes()) for v in sorted(files) if base<v<=target]

    def run():
        c=expand_connection(dsn,application_name)
        try:
            if identity is not None and c.execute(EXPAND_IDENTITY).fetchone()['identity']!=identity:
                raise ExpandError('identity_mismatch')
            if not c.execute('SELECT pg_try_advisory_xact_lock(74509137) AS locked').fetchone()['locked']:
                raise ExpandError('busy')
            if ledger_version(c)!=base: raise ExpandError('ledger_mismatch')
            applied={r['version']:r['sha256'] for r in c.execute('SELECT version,sha256 FROM archive_migrations')}
            if applied!={v:hashlib.sha256(p.read_bytes()).hexdigest() for v,p in files.items() if 1<v<=base}:
                raise ExpandError('ledger_mismatch')
            # An unqualified name resolves in pg_catalog before public (search_path=public), so a catalog name is
            # never a table here. A GRANT target is an ordinary or partitioned table of `public`; a new table may
            # not reuse the name of any relation (a sequence or view included), or IF NOT EXISTS would skip it.
            relations={(r['nspname'],r['relname'],r['relkind']) for r in c.execute(
                "SELECT n.nspname,c.relname,c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname IN ('public','pg_catalog')")}
            taken={name for _,name,_ in relations}
            inventory={name for space,name,kind in relations if space=='public' and kind in ('r','p')}-{
                name for space,name,_ in relations if space=='pg_catalog'}
            created={}
            for _,data in pending:
                text=data.decode()
                if rollout.unattended_class(text,inventory|set(created))!='expand': raise ExpandError('not_unattended')
                following=rollout.new_tables(text,created)
                if (set(following)-set(created))&taken: raise ExpandError('not_unattended')
                created=following
            for old,new in appended.values():
                if rollout.role_class(old,new,created,tables=inventory)!='additive': raise ExpandError('role_not_additive')
            for version,data in pending:
                c.execute(data.decode())
                c.execute('INSERT INTO archive_migrations(version,sha256) VALUES(%s,%s)',(version,hashlib.sha256(data).hexdigest()))
                c.execute("UPDATE probe_metadata SET value=%s WHERE name='schema_version'",(str(version),))
            # Each claimed new table must now be an ordinary or partitioned table of `public`: a relation the batch
            # made under that name some other way (a sequence, an index) made IF NOT EXISTS skip it (Sol r3).
            if created and c.execute("SELECT count(*) AS n FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname='public' AND c.relkind IN ('r','p') AND c.relname=ANY(%s)",(sorted(created),)
                    ).fetchone()['n']!=len(created):
                raise ExpandError('not_unattended')
            for old,new in appended.values():
                if rollout.statements(new[len(old):]): c.execute(new[len(old):])
            if before_commit: before_commit(c)
            c.commit()
        finally: c.close()
        return {'schema_version':target}
    return expand_code(run)


def ledger_version(c):
    if not c.execute("SELECT to_regclass('public.probe_metadata') IS NOT NULL AS present").fetchone()['present']:
        raise ExpandError('ledger_mismatch')
    row=c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()
    if not row: raise ExpandError('ledger_mismatch')
    return int(row['value'])


REPLAY_LOCK_TABLES=('messages','deliveries','download_grants')


def lock_replay_tables(c, seconds=2):
    # schema.sql's CREATE INDEX IF NOT EXISTS needs SHARE on these. Take them together and never queue:
    # a queued request holds new writers behind it, and locks taken one by one stay held while the
    # next one waits, so short waits could add up past a writer's 5 s budget. A table missing from a
    # partial restore is left to the replay to recreate.
    present=[t for t in REPLAY_LOCK_TABLES
             if c.execute('SELECT to_regclass(%s) IS NOT NULL AS present',('public.'+t,)).fetchone()['present']]
    if not present: return
    statement='LOCK TABLE '+', '.join(present)+' IN SHARE MODE NOWAIT'
    deadline=time.monotonic()+seconds
    while True:
        try:
            with c.transaction():
                c.execute(statement)
            return
        except LockNotAvailable:
            if time.monotonic()>=deadline: raise
            time.sleep(0.05)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


KEY_OPERATION_SCHEMA=44  # api_key_operations (ANYM-495)
RUNTIME_SCHEMA=67  # the newest schema this runtime knows; migrate() refuses a newer database


def revocable_token(value):
    # What revoke accepts by token; --check runs the same test (ANYM-495).
    if not isinstance(value,str) or not 32<=len(value)<=256:raise ValueError('invalid_key')
    return value
def caller_token(value):
    # A caller-made token (ANYM-495): printable ASCII without spaces, as authentication reads it.
    if not isinstance(value,str) or not re.fullmatch(r'[!-~]{32,256}',value):raise ValueError('invalid_key')
    return value


def operation_name(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9._/-]{1,128}',value):
        raise ValueError('invalid_operation_id')
    return value


def issue_request(principal,accounts,days,chats,permissions,key_permissions,key_of=None):
    # The request as given, so a replay with other arguments is refused even when the result would match.
    def given(value):return 'unset' if value is UNSET else None if value is None else sorted(value)
    return hashlib.sha256(json.dumps([principal,sorted(accounts or []),days,given(chats),given(permissions),
        given(key_permissions)]+([['key_permissions_of',key_of]] if key_of else [])).encode()).hexdigest()


class OperationTaken(Exception):
    # ANYM-495: the operation ID or the caller's token is held by a committed key; the transaction rolls back.
    pass


def key_operation(c,operation_id):
    # No foreign key (044 takes no lock on api_keys): an operation without its key row still holds the ID.
    return c.execute('''SELECT k.id,k.principal,k.expires_at,k.revoked,k.digest,o.operation_request
        FROM api_key_operations o LEFT JOIN api_keys k ON k.id=o.key_id WHERE o.operation_id=%s''',
        (operation_id,)).fetchone()


def schema_version(c):
    return int(c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value'])


def require_schema(c,version):
    if schema_version(c)<version:raise RuntimeError('schema_not_ready')


def bounded(value: int, maximum: int = 100) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise APIError('invalid_limit')
    return value


def date_bound(value: str) -> datetime | None:
    if value=='':
        return None
    if not isinstance(value,str):raise APIError('invalid_utc_date')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('timezone required')
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        raise APIError('invalid_utc_date') from None


def serial(rows):
    if isinstance(rows, dict):
        return {k: serial(v) for k, v in rows.items()}
    if isinstance(rows, list):
        return [serial(v) for v in rows]
    if isinstance(rows, datetime):
        return rows.isoformat()
    if isinstance(rows, UUID):
        return str(rows)
    return rows


class Store:
    def __init__(self, dsn: str, files: Path, public_url: str):
        from .security_logging import redact_library_logs
        redact_library_logs()
        self._status_cache=OrderedDict()
        self._status_cache_lock=threading.Lock()
        self.files = files.resolve()
        self.public_url = public_url.rstrip('/')
        # Keepalives and tcp_user_timeout turn a half-open connection into a
        # prompt OperationalError instead of a call that outlives the lease.
        # No server-side prepared statements: a cached SELECT * plan fails with "cached plan must
        # not change result type" once a migration adds a column under a running service (086304e6).
        self.pool = ConnectionPool(dsn, min_size=1, max_size=6, timeout=5, open=True,
            kwargs={'row_factory': dict_row, 'prepare_threshold': None, 'connect_timeout': 5,
                    'keepalives': 1, 'keepalives_idle': 30, 'keepalives_interval': 10,
                    'keepalives_count': 3, 'tcp_user_timeout': 30000,
                    'options': '-c statement_timeout=5000 -c timezone=UTC'})
        self.pool.wait(timeout=10)

    def close(self):
        self.pool.close()

    @staticmethod
    def identity(c, token: str):
        if not isinstance(token, str) or not 32 <= len(token) <= 256:
            raise APIError('unauthorized', 401)
        return Store.identity_digest(c, digest(token))

    @staticmethod
    def identity_digest(c, key_digest, *, missing='unauthorized', status=401):
        known = c.execute('SELECT principal,id FROM api_keys WHERE digest=%s', (key_digest,)).fetchone()
        if not known:
            raise APIError(missing, status)
        lock_principal(c, known['principal'], shared=True)
        # READ COMMITTED rechecks after a concurrent operator changed access.
        # clock_timestamp also rejects credentials that expired while waiting.
        row = c.execute('''SELECT p.*, k.digest AS key_digest,k.id AS key_id,
            k.permissions AS key_permissions,k.permission_version AS key_permission_version,
            k.expires_at AS key_expires_at FROM api_keys k
            JOIN principals p ON p.id=k.principal
            WHERE k.digest=%s AND NOT k.revoked AND k.expires_at>clock_timestamp() AND p.enabled''',
            (key_digest,)).fetchone()
        if not row:
            error = APIError(missing, status)
            error.audit_identity = {'id': known['principal'], 'key_id': known['id']}
            raise error
        row['principal_permissions'] = row['permissions']
        row['permissions'] = effective_permissions(row['permissions'], row['key_permissions'])
        return row

    @contextmanager
    def access(self, operation, resolve_identity):
        failure = None
        p = None
        late = None
        try:
            with self.pool.connection() as c:
                with c.transaction():
                    try:
                        with c.transaction():
                            p = resolve_identity(c)
                            permission = OPERATION_PERMISSIONS[operation] if operation else None
                            if permission and permission not in p['permissions']:
                                raise APIError('forbidden', 403)
                            from .result_evidence import FILES_ROOT
                            root = FILES_ROOT.set(self.files)
                            try:
                                yield c, p
                            finally:
                                FILES_ROOT.reset(root)
                    except Exception as error:
                        # Roll back the attempted action, but commit its denial audit
                        # outside the savepoint. Successful mutations + audit commit
                        # atomically; a broken audit sink never becomes silent success.
                        from .error_details import classified
                        code=classified(error)
                        failure = APIError(code,503) if code in ('query_timeout','service_busy','service_unavailable') else error
                        # ANYM-665: a reviewed recovery hint set on the database error survives the conversion.
                        if failure is not error and hasattr(error,'next_action'):failure.next_action=error.next_action
                        p = p or getattr(error, 'audit_identity', None)
                        # transaction_timeout ends the server session (ANYM-265), and so does a lost connection: nothing
                        # was committed, and the failure is audited on a fresh connection instead of the closed one, so
                        # a known refusal (ANYM-748: chat_guard_unavailable, nothing queued) is never masked.
                        late = c.closed
                    if operation:
                        outcome = 'ok' if failure is None else ('denied' if isinstance(failure, APIError) else 'error')
                        entry = (p, operation, outcome,
                                 failure.code if isinstance(failure, APIError) else ('operation_failed' if failure else None))
                        if not late:
                            audit(c, *entry)
        except TransactionTimeout:
            # The audit write or the commit ran past transaction_timeout: the server rolled the whole
            # transaction back and closed the session, so the timeout is audited on a fresh connection.
            if not operation:
                raise
            failure, late = APIError('query_timeout', 503), True
            entry = (p, operation, 'denied', 'query_timeout')
        if late and operation:
            with self.pool.connection() as fresh:
                audit(fresh, *entry)
        if failure is not None:
            failure.operation=operation
            if isinstance(failure,APIError) and operation:failure.audited=True
            raise failure

    def authorized(self, token, operation=None):
        return self.access(operation, lambda c: self.identity(c, token))

    def audit_boundary(self, code):
        with self.pool.connection() as c:
            audit(c, None, 'http_boundary', 'denied', code)

    def authenticate(self, token):
        with self.authorized(token) as (_, p):
            return p['id']

    def permits(self, token, operation):
        """Whether this key holds `operation`'s permission: the body boundary's question, not a denial to audit."""
        with self.authorized(token) as (_, p):
            return OPERATION_PERMISSIONS[operation] in p['permissions']

    @staticmethod
    def dataset(c,p):
        if p['chat_ids'] is not None:
            # Account-wide last_ingest_at/detail may change only because of a
            # hidden chat. Do not disclose those aggregates to a chat-scoped key.
            rows=c.execute('''SELECT a.id,a.synthetic,'chat_scope_only' AS coverage,
                NULL AS status,NULL AS last_ingest_at,NULL AS detail_code FROM accounts a
                WHERE a.id=ANY(%s) AND EXISTS(SELECT 1 FROM chats c
                    WHERE c.account=a.id AND c.id=ANY(%s)) ORDER BY a.id''',
                (p['accounts'],p['chat_ids'])).fetchall()
            return {'synthetic':all(r['synthetic'] for r in rows),'sources':rows,'coverage':'chat_scope_only'}
        rows=c.execute('''SELECT a.id,a.synthetic,coalesce(s.coverage,'synthetic_fixture_only') AS coverage,
            s.status,s.last_ingest_at,s.detail_code FROM accounts a LEFT JOIN source_status s ON s.account=a.id
            WHERE a.id=ANY(%s) ORDER BY a.id''',(p['accounts'],)).fetchall()
        from .conflict_classes import grouped,report
        conflicts=grouped(c,'account',p['accounts'])
        for row in rows:
            open_by_class,row['active_conflicts'],row['historical_conflicts']=conflicts.get(row['id'],({},0,0))
            row['detail_scope']='account_summary_not_this_result'
            if row['detail_code']=='version_conflict' and not row['active_conflicts']:
                row['detail_code']='coverage_not_reconciled'
            from .status_details import describe
            row['detail']=describe(row['detail_code'])
            if row['detail_code']=='version_conflict':row['detail']['next_action']=report(open_by_class)['next_step']
        return serial({'synthetic':all(r['synthetic'] for r in rows),'sources':rows,
                       'coverage':'partial_or_unknown' if any(r['status'] for r in rows) else 'synthetic_fixture_only'})

    @staticmethod
    def message(c, p, mid):
        if not isinstance(mid,str) or not 1<=len(mid)<=256:
            raise APIError('not_found',404)
        where,args=scope_filter(p)
        row = c.execute('SELECT * FROM messages WHERE id=%s AND '+where, [mid,*args]).fetchone()
        if not row:
            raise APIError('not_found', 404)

        for key in list(row):
            if key=='document' or key.startswith('document_'):row.pop(key)
        targets(p,[row['id']])
        return row

    def capabilities(self, token, detail='full', sections=None):
        from .capabilities import capabilities
        return capabilities(self, token, detail, sections)

    def preview_message(self,token,account,chat_id,text,reply_to='',format='plain',
                        link_preview=False,allow_lossy=False):
        from .outgoing import preview
        with self.authorized(token,'preview_message') as (c,p):
            return preview(c,p,account,chat_id,text,reply_to,format,link_preview,allow_lossy)

    def send_message(self,token,account,chat_id,text,request_id,reply_to='',format='plain',
                     preview_digest='',link_preview=False,allow_lossy=False,
                     expected_last_message_id=None,guard=None,allow_duplicate=None,duplicate_guard=False):
        from .outgoing import enqueue
        with self.authorized(token,'send_message') as (c,p):
            return enqueue(c,p,account,chat_id,text,request_id,reply_to,format,
                preview_digest,link_preview,allow_lossy,expected_last_message_id=expected_last_message_id,guard=guard,
                duplicate_guard=duplicate_guard,allow_duplicate=allow_duplicate)

    # ANYM-532: a phone recipient; phone_send.preview and phone_send.send wait between these short reads.
    def phone_preview_lookup(self,token,account,phone,text,reply_to='',format='plain',
                             link_preview=False,allow_lossy=False):
        from .phone_send import preview_lookup
        with self.authorized(token,'preview_message') as (c,p):
            return preview_lookup(self,c,p,account,phone,text,reply_to,format,link_preview,allow_lossy)

    def phone_preview_result(self,token,account,phone,operation_id,text,reply_to='',format='plain',
                             link_preview=False,allow_lossy=False):
        from .phone_send import preview_result
        with self.authorized(token,'preview_message') as (c,p):
            return preview_result(self,c,p,account,phone,operation_id,text,reply_to,format,link_preview,allow_lossy)

    def phone_send_begin(self,token,account,phone,text,request_id,reply_to='',format='plain',
                         preview_digest='',link_preview=False,allow_lossy=False):
        from .phone_send import send_begin
        with self.authorized(token,'send_message') as (c,p):
            return send_begin(self,c,p,account,phone,text,request_id,reply_to,format,preview_digest,
                link_preview,allow_lossy)

    def phone_send_finish(self,token,account,phone,operation_id,text,request_id,reply_to='',format='plain',
                          preview_digest='',link_preview=False,allow_lossy=False):
        from .phone_send import send_finish
        with self.authorized(token,'send_message') as (c,p):
            return send_finish(self,c,p,account,phone,operation_id,text,request_id,reply_to,format,
                preview_digest,link_preview,allow_lossy)

    # ANYM-643: a Telegram username recipient; username_send.preview and username_send.send wait between these.
    def username_preview_lookup(self,token,account,username,text,reply_to='',format='plain',
                                link_preview=False,allow_lossy=False):
        from .username_send import preview_lookup
        with self.authorized(token,'preview_message') as (c,p):
            return preview_lookup(self,c,p,account,username,text,reply_to,format,link_preview,allow_lossy)

    def username_preview_result(self,token,account,username,operation_id,text,reply_to='',format='plain',
                                link_preview=False,allow_lossy=False):
        from .username_send import preview_result
        with self.authorized(token,'preview_message') as (c,p):
            return preview_result(self,c,p,account,username,operation_id,text,reply_to,format,link_preview,allow_lossy)

    def username_send_begin(self,token,account,username,text,request_id,reply_to='',format='plain',
                            preview_digest='',link_preview=False,allow_lossy=False):
        from .username_send import send_begin
        with self.authorized(token,'send_message') as (c,p):
            return send_begin(self,c,p,account,username,text,request_id,reply_to,format,preview_digest,
                link_preview,allow_lossy)

    def username_send_finish(self,token,account,username,operation_id,text,request_id,reply_to='',format='plain',
                             preview_digest='',link_preview=False,allow_lossy=False):
        from .username_send import send_finish
        with self.authorized(token,'send_message') as (c,p):
            return send_finish(self,c,p,account,username,operation_id,text,request_id,reply_to,format,
                preview_digest,link_preview,allow_lossy)

    # ANYM-546: messages_send; send_intent.send waits between these short transactions.
    def messages_send_preview(self,token,arguments,draft=None):
        from .send_intent import _text, open_grants, payload, resolve, save_preview
        from .resolve_target import resolve_target
        with self.authorized(token,'preview_message') as (c,p):
            value=payload(arguments)
            # ANYM-643: a username draft also names the person it resolved to.
            text=draft and {**_text(draft),**{k:draft[k] for k in ('name','user_id') if 'username' in draft}}
            resolution=resolve(self,c,p,value,phone_text=text or None,open_chat=open_grants(arguments),
                               files=arguments.get('allow_files') is not False)
            if resolution is not None:return save_preview(c,p,value,resolution)
            target=resolve_target(self,c,p,value['to'],'message',value['account'])['target']
            kind='username' if 'username' in target else 'phone'
            return {kind:{'account':target['account'],kind:target[kind][1:],'text':value['text'],
                          'reply_to':'','format':value['format']}}

    def messages_send_execute(self,token,arguments):
        from .send_intent import admit, advance, open_grants
        with self.authorized(token,'send_message') as (c,p):
            intent=admit(self,c,p,arguments)
            # preview_stale: its fresh preview commits with this transaction, then the caller raises.
            if isinstance(intent,APIError):return {'raise':intent}
            return advance(self,c,p,intent,resume=arguments.get('resume',False),first=True,
                           open_chat=open_grants(arguments),
                           files=arguments.get('allow_files') is not False)

    def messages_send_advance(self,token,intent_id,arguments):
        from .send_intent import advance, open_grants
        with self.authorized(token,'send_message') as (c,p):
            return advance(self,c,p,intent_id,open_chat=open_grants(arguments),
                           files=arguments.get('allow_files') is not False)

    def get_send_status(self,token,send_id=None,request_id=None):
        from .outgoing import status
        with self.authorized(token,'get_send_status') as (c,p):
            # A send_id call keeps the old call shape; only a request_id lookup (ANYM-736) names it.
            return status(c,p,send_id) if request_id is None else status(c,p,send_id,request_id)

    def prepare_file_upload(self,token,account,chat_id,filename,mime,bytes,sha256,request_id):
        from .uploads import prepare
        with self.authorized(token,'prepare_file_upload') as (c,p):
            return prepare(self,c,p,account,chat_id,filename,mime,bytes,sha256,request_id)

    def upload_file_chunk(self,token,upload_id,offset,data_base64):
        from .uploads import chunk
        with self.authorized(token,'upload_file_chunk') as (c,p):
            return chunk(self,c,p,upload_id,offset,data_base64)

    def get_file_upload(self,token,upload_id):
        from .uploads import status
        with self.authorized(token,'get_file_upload') as (c,p):
            return status(self,c,p,upload_id)

    def cleanup_file_uploads(self):
        """API-owned maintenance; retains staged sources of pending sends."""
        from .uploads import cleanup
        with self.pool.connection() as c:
            cleanup(self,c)

    def list_messenger_operations(self,token,account):
        from .commands import manifest
        with self.authorized(token,'list_messenger_operations') as (c,p):
            return manifest(c,p,account)

    def preview_messenger_operation(self,token,account,operation,chat_id='',message_id='',
                                   expected_revision=0,parameters=None):
        from .commands import preview
        with self.authorized(token,'preview_messenger_operation') as (c,p):
            return preview(self,c,p,account,operation,chat_id,message_id,expected_revision,parameters)

    def execute_messenger_operation(self,token,account,operation,chat_id='',message_id='',
                                   expected_revision=0,parameters=None,request_id='',preview_digest=''):
        from .commands import enqueue
        with self.authorized(token,'execute_messenger_operation') as (c,p):
            return enqueue(self,c,p,account,operation,chat_id,message_id,expected_revision,
                           parameters,request_id,preview_digest)

    def get_messenger_operation(self,token,operation_id):
        from .commands import status
        with self.authorized(token,'get_messenger_operation') as (c,p):
            return status(self,c,p,operation_id)

    def set_messenger_operations(self,account,operations):
        """Owner-only account opt-in; this method is not exposed through MCP."""
        from .messenger_operations import operation_manifest
        from .whatsapp_capabilities import account_provider
        values=normalize_list(operations,maximum=100)
        with self.pool.connection() as c:
            row=c.execute('SELECT platform FROM accounts WHERE id=%s',(account,)).fetchone()
            if not row:raise ValueError('unknown_account')
            supported={entry['operation'] for entry in operation_manifest(row['platform'],provider=account_provider(c,account,row['platform'])) if entry['supported']}
            if set(values)-supported:raise ValueError('unsupported_operation')
            changed=c.execute('UPDATE account_policies SET operations_enabled=%s WHERE account=%s',
                              (values,account)).rowcount
            if not changed:raise ValueError('unknown_account')
            audit(c,None,'set_operations','ok')

    def list_chats(self, token, after='', limit=25, *, accounts=None,account='',query='',chat_types=None,
                   order='id',exclude_channels=False,awaiting_reply=False,from_time='',to_time='',
                   include_coverage=True,awaiting_reply_days=14,exclude_chats=None,exclude_senders=None,
                   only_questions=False,only_unread=False,view='archive',exclude_likely_broadcasts=False,
                   include_muted=False,promotional='exclude',my_roles=None,can_post=None,
                   awaiting_their_reply=False,min_wait_hours=None,courtesies='include'):
        from .chat_queries import list_chats
        from .message_queries import hint_forward_origin
        from .search_query import fold_account
        with self.authorized(token,'list_chats') as (c,p):
            accounts=fold_account(account,accounts)
            result=list_chats(self,c,p,after,limit,accounts=accounts,query=query,chat_types=chat_types,
                order=order,exclude_channels=exclude_channels,awaiting_reply=awaiting_reply,
                from_time=from_time,to_time=to_time,include_coverage=include_coverage,
                awaiting_reply_days=awaiting_reply_days,exclude_chats=exclude_chats,exclude_senders=exclude_senders,
                only_questions=only_questions,only_unread=only_unread,view=view,
                exclude_likely_broadcasts=exclude_likely_broadcasts,include_muted=include_muted,promotional=promotional,
                my_roles=my_roles,can_post=can_post,awaiting_their_reply=awaiting_their_reply,min_wait_hours=min_wait_hours,
                courtesies=courtesies)
            hint_forward_origin(c,result['items'])
            return result

    def dismiss_reply(self,token,chat_id,message_id,expected_revision,reason=None,note=None):
        from .reply_state import dismiss
        with self.authorized(token,'dismiss_reply') as (c,p):
            return dismiss(self,c,p,chat_id,message_id,expected_revision,reason,note)

    def snooze_reply(self,token,chat_id,message_id,expected_revision,until,note=None):
        from .reply_state import snooze
        with self.authorized(token,'snooze_reply') as (c,p):
            return snooze(self,c,p,chat_id,message_id,expected_revision,until,note)

    def restore_reply(self,token,chat_id,message_id,expected_revision):
        from .reply_state import restore
        with self.authorized(token,'restore_reply') as (c,p):
            return restore(self,c,p,chat_id,message_id,expected_revision)

    def mute_reply_source(self,token,chat_id=None,account=None,sender_id=None,reason=None,note=None):
        from .reply_state import mute
        with self.authorized(token,'mute_reply_source') as (c,p):
            return mute(self,c,p,chat_id,account,sender_id,reason,note)

    def unmute_reply_source(self,token,chat_id=None,account=None,sender_id=None):
        from .reply_state import unmute
        with self.authorized(token,'unmute_reply_source') as (c,p):
            return unmute(self,c,p,chat_id,account,sender_id)

    def list_reply_dismissals(self,token,kind='dismissed',after='',limit=20):
        from .reply_state import list_entries
        with self.authorized(token,'list_reply_dismissals') as (c,p):
            return list_entries(self,c,p,kind,after,limit)

    def search_people(self,token,query='',*,accounts=None,chats=None,after='',limit=20,
                      transliterate=False,group_by_sender=False,person='',account=''):
        from .people_candidates import search_people
        from .search_query import fold_account
        with self.authorized(token,'search_people') as (c,p):
            accounts=fold_account(account,accounts)
            if type(transliterate) is not bool or type(group_by_sender) is not bool:
                raise APIError('invalid_filter')
            return search_people(self,c,p,query,accounts=accounts,chats=chats,after=after,limit=limit,
                transliterate=transliterate,group_by_sender=group_by_sender,person=person)

    def search_files(self,token,query='',account='',chat='',sender='',from_time='',to_time='',
                     after='',limit=20,*,accounts=None,chats=None,senders=None,query_mode='all',
                     order='asc',before='',direction='',filename='',search_language='auto',include_coverage=True,
                     media_kind=None,ready=None):
        from .file_index import search_files
        with self.authorized(token,'search_files') as (c,p):
            return search_files(self,c,p,query,account,chat,sender,from_time,to_time,after,limit,
                accounts=accounts,chats=chats,senders=senders,query_mode=query_mode,order=order,
                before=before,direction=direction,filename=filename,search_language=search_language,
                include_coverage=include_coverage,media_kind=media_kind,ready=ready)

    def get_file_text(self,token,attachment_id,offset=0,limit=4000,content=None,compact_spans=False):
        from .file_index import get_file_text
        with self.authorized(token,'get_file_text') as (c,p):
            return get_file_text(self,c,p,attachment_id,offset=offset,limit=limit,content=content,compact_spans=compact_spans)

    def get_file_occurrences(self,token,attachment_id,after='',limit=20):
        from .file_index import get_file_occurrences
        with self.authorized(token,'get_file_occurrences') as (c,p):
            return get_file_occurrences(self,c,p,attachment_id,after=after,limit=limit)

    def get_file_segments(self,token,attachment_id,offset=0,limit=100):
        from .file_index import get_file_segments
        with self.authorized(token,'get_file_segments') as (c,p):
            return get_file_segments(self,c,p,attachment_id,offset=offset,limit=limit)

    def get_sync_status(self,token,chat_id='',from_time='',to_time='',refresh=True):
        from .sync_status import get_sync_status
        with self.authorized(token,'get_sync_status') as (c,p):
            if type(refresh) is not bool:raise APIError('invalid_filter')
            if chat_id:
                from .chat_evidence import chat_statuses
                lower,upper=date_bound(from_time),date_bound(to_time)
                if lower and upper and lower>=upper:raise date_range_refusal()
                if not isinstance(chat_id,str) or len(chat_id)>256:raise APIError('invalid_filter')
                data=chat_statuses(c,p,[chat_id],lower,upper,conflict_items=True)
                if not data:raise APIError('not_found',404)
                targets(p,[chat_id]);return serial(data[chat_id])
            if from_time or to_time:raise APIError('chat_required')
            cache_key=(p['key_id'],p['scope_version'])
            now=time.monotonic()
            with self._status_cache_lock:
                cached=self._status_cache.get(cache_key)
                if not refresh and cached and now-cached[0]<10:
                    self._status_cache.move_to_end(cache_key)
                    result=json.loads(cached[1])
                    result['cache']={'hit':True,'age_seconds':round(now-cached[0],3),'max_age_seconds':10}
                    return result
            result=serial(get_sync_status(c,p))
            result['observed_at']=datetime.now(timezone.utc).isoformat()
            with self._status_cache_lock:
                self._status_cache[cache_key]=(time.monotonic(),json.dumps(result))
                self._status_cache.move_to_end(cache_key)
                if len(self._status_cache)>128:self._status_cache.popitem(last=False)
            result['cache']={'hit':False,'age_seconds':0,'max_age_seconds':10}
            return result

    def resolve_reference(self,token,account,provider_chat_id,provider_message_id=''):
        from .chat_evidence import resolve_chat,chat_statuses
        from .message_queries import enrich
        from .search_query import valid_text
        with self.authorized(token,'resolve_reference') as (c,p):
            chat=resolve_chat(c,p,account,provider_chat_id)
            valid_text(provider_message_id,256)
            result={'chat':chat,'sync':chat_statuses(c,p,[chat['id']])[chat['id']]}
            if provider_message_id:
                row=c.execute('SELECT id FROM messages WHERE account=%s AND chat=%s AND provider_id=%s',
                    (account,chat['id'],provider_message_id)).fetchone()
                if not row:raise APIError('not_found',404)
                message=self.message(c,p,row['id']);enrich(c,[message],p);result['message']=message
            return serial(result)

    def resolve_target(self,token,to,purpose='message',account='',sender=False):
        from .resolve_target import resolve_target
        # Audited as resolve_reference (no migration for the audit_log CHECK); same read permission.
        with self.authorized(token,'resolve_reference') as (c,p):
            return serial(resolve_target(self,c,p,to,purpose,account,sender))

    def search(self, token, query='', account='', chat='', sender='', from_time='',
               to_time='', after='', limit=20, *, accounts=None, chats=None, senders=None, collapse_forwards=True,
               query_mode='all', order='asc', before='', direction='', search_language='auto',
               fuzzy=False, compact=False, body_chars=None, include_coverage=True,view='archive',include_system=False,exclude_channels=False,
               chat_types=None,include_channels=False,person='',body_format='',include_media_text=True,_scope=None):
        # None is the internal no-default sentinel of message_queries.search, never a public value.
        if type(include_channels) is not bool:raise APIError('invalid_filter')
        from .message_queries import named_chats
        from .live_search import scope
        with self.authorized(token,'search') as (c,p):
            # ANYM-478: the live search keeps the scope this page was read under (F-478a); never in the answer.
            if isinstance(_scope,list):_scope.append(scope(p))
            named_chats(c,p,chat,chats)
            result=self._search(c,p,query,account,chat,sender,from_time,to_time,after,limit,
                accounts=accounts,chats=chats,senders=senders,collapse_forwards=collapse_forwards,query_mode=query_mode,
                order=order,before=before,direction=direction,search_language=search_language,
                fuzzy=fuzzy,compact=compact,body_chars=body_chars,include_coverage=include_coverage,view=view,include_system=include_system,exclude_channels=exclude_channels,
                chat_types=chat_types,include_channels=include_channels,person=person,body_format=body_format,include_media_text=include_media_text)
            self._search_suggestion(c,p,account,accounts,chat,chats,chat_types,exclude_channels,result)
            return result

    @staticmethod
    def _search_suggestion(c,p,account,accounts,chat,chats,chat_types,exclude_channels,result):
        from .live_search import suggest
        # ANYM-478: a thin archive answer names the call that may find more; the hint is optional, so its
        # failure (in a savepoint) never costs the archive answer.
        try:
            with c.transaction():
                suggest(c,p,{'account':account,'accounts':accounts,'chat':chat,'chats':chats,'chat_types':chat_types,
                    'exclude_channels':exclude_channels},result['result_evidence'])
        except Exception as error:
            import logging
            logging.getLogger(__name__).warning('search_suggestion_failed exception_type=%s',type(error).__name__)

    def live_search_plan(self,token,arguments,*,paged,request_id):
        """ANYM-478: per-account live entries and the operations to queue; reads only."""
        from .live_search import plan
        with self.authorized(token,'search') as (c,p):
            return plan(c,p,arguments,paged=paged,request_id=request_id)

    def live_search_scope(self,token):
        """ANYM-478: the caller's current search scope, checked as the archive search checks it."""
        from .live_search import scope
        with self.authorized(token,'search') as (_,p):
            return scope(p)

    def live_search_receipts(self,token,operation_ids):
        """ANYM-478: this key's receipts of its live searches, one short read; a missing one is left out."""
        from .commands import status
        result={}
        with self.authorized(token) as (c,p):
            for operation_id in operation_ids:
                try:
                    with c.transaction():result[operation_id]=status(self,c,p,operation_id)
                except APIError:pass
        return result

    def _search(self,c,p,*args,**kwargs):
        from .message_queries import search
        return search(self,c,p,*args,**kwargs)

    def get_messages(self, token, chat_id, after='', limit=20, *, before='', order='asc',
                     from_time='',to_time='',compact=False,body_chars=None,include_coverage=True,view='archive',include_system=False,body_format=''):
        with self.authorized(token,'get_messages') as (c,p):
            if not isinstance(chat_id,str) or not 1<=len(chat_id)<=256:raise APIError('not_found',404)
            return self._search(c,p,chat=chat_id,after=after,limit=limit,before=before,
                order=order,from_time=from_time,to_time=to_time,compact=compact,
                body_chars=body_chars,include_coverage=include_coverage,view=view,include_system=include_system,body_format=body_format)

    def get_context(self, token, message_id, limit=10, *, before_count=None,after_count=None,
                    compact=False,body_chars=None,include_coverage=True,view='archive',include_system=False,body_format=''):
        from .search_query import coverage,output_options
        from .formatting_readback import option
        body_format=option(body_format)
        from .message_queries import history_snapshot
        from .conversation import options
        options(view,include_system)
        conversation=view=='conversation'
        with self.authorized(token,'get_context') as (c,p):
            bounded(limit,50)
            cap=output_options(compact,body_chars,include_coverage)
            explicit,before_count,after_count=self._context_counts(before_count,after_count)
            anchor,snapshot=self._context_anchor(c,p,message_id,conversation,include_system,body_format)
            scans=self._context_scans(c,anchor,conversation,include_system,explicit,limit,before_count,after_count)
            rows,older,newer=self._context_rows(c,p,anchor,scans,conversation,include_system,explicit,limit,compact,cap,body_format)
            from .sender_names import anchor as name_anchor
            name_anchor(c,p,anchor)  # ANYM-831: the anchor only, after its enrichment
            files=[f for f in anchor['attachments'] if f['download_status']=='ready']
            jobs=[f for f in anchor['attachments'] if f['download_status']!='ready']
            from .reviews import list_reviews
            reviews=list_reviews(c,p,message_id,limit=5)
            window=self._context_window(rows,message_id,explicit,older,newer,scans,before_count,after_count)
            from .chat_evidence import chat_statuses
            extra=self._context_conversation(c,p,anchor,rows,window,snapshot,include_system,conversation)
            result=serial({'message':anchor,'context':rows,'context_window':window,
                'attachments':files,'pending_attachments':jobs,'reviews':reviews,
                **extra,'chat_sync':chat_statuses(c,p,[anchor['chat']])[anchor['chat']] if include_coverage else None,
                **coverage(self,c,p,include_coverage)})
            if conversation and history_snapshot(c,p,anchor['chat'],include_system=include_system)!=snapshot:
                raise APIError('cursor_changed',409)
            return result

    @staticmethod
    def _context_counts(before_count,after_count):
        explicit=before_count is not None or after_count is not None
        if explicit:
            before_count=5 if before_count is None else before_count
            after_count=5 if after_count is None else after_count
            if any(type(v) is not int or not 0<=v<=50 for v in (before_count,after_count)):
                raise APIError('invalid_limit')
        return explicit,before_count,after_count

    def _context_anchor(self,c,p,message_id,conversation,include_system,body_format=''):
        from .message_queries import enrich
        anchor=self.message(c,p,message_id)
        snapshot=None
        if conversation:
            from .message_queries import history_snapshot
            snapshot=history_snapshot(c,p,anchor['chat'],include_system=include_system)
            # Authorization remains READ COMMITTED. Re-read source evidence
            # after pinning the view, then reject changes around all scans.
            anchor=self.message(c,p,message_id)
            enrich(c,[anchor],p,with_body=bool(body_format))
        return anchor,snapshot

    @staticmethod
    def _context_scans(c,anchor,conversation,include_system,explicit,limit,before_count,after_count):
        from .conversation import order_terms,visible_sql
        provider=str(anchor['provider_id'])
        numeric=int(provider) if anchor.get('platform')=='telegram' and provider.isascii() and provider.isdigit() and len(provider)<=18 else 0
        scans=[]
        for op,sort,count in [('<','DESC',before_count if explicit else limit),
                              ('>','ASC',after_count if explicit else limit)]:
            if conversation:
                terms=order_terms('m')
                selected=c.execute('SELECT m.id,m.account,m.chat,m.sender,m.body,m.sent_at,m.revision,m.direction '
                    'FROM messages m LEFT JOIN message_metadata cmd ON cmd.message=m.id AND cmd.revision=m.revision '
                    'WHERE m.account=%s AND m.chat=%s AND NOT m.deleted AND '+
                    (visible_sql('m','cmd')+' AND ' if not include_system else '')+
                    '('+','.join(terms)+')'+op+'(%s,%s,%s) ORDER BY '+
                    ','.join(t+' '+sort for t in terms)+' LIMIT %s',
                    (anchor['account'],anchor['chat'],anchor['sent_at'],numeric,anchor['id'],count+1)).fetchall()
            else:
                selected=c.execute('SELECT id,account,chat,sender,body,sent_at,revision,direction FROM messages '
                    'WHERE account=%s AND chat=%s AND NOT deleted AND (sent_at,id)'+op+'(%s,%s) '
                    'ORDER BY sent_at '+sort+',id '+sort+' LIMIT %s',
                    (anchor['account'],anchor['chat'],anchor['sent_at'],anchor['id'],count+1)).fetchall()
            scans.append((selected[:count],len(selected)>count))
        return scans

    @staticmethod
    def _context_rows(c,p,anchor,scans,conversation,include_system,explicit,limit,compact,cap,body_format=''):
        from .message_queries import enrich
        from .search_query import shape_messages
        from .conversation import cap_display_text,sort_key,decorate_messages
        older,newer=scans[0][0],scans[1][0]
        center=[] if anchor['deleted'] or (conversation and not include_system and (anchor.get('is_service') or anchor.get('message_kind')=='system')) else [{key:anchor[key] for key in
            ('id','account','chat','sender','body','sent_at','revision','direction')}]
        rows=older+center+newer
        if not explicit:
            rows=sorted(rows,key=lambda r:(r['id']!=anchor['id'],
                abs((r['sent_at']-anchor['sent_at']).total_seconds())))[:limit]
        if conversation:
            # Excerpts spend their budget in the returned order (ANYM-490).
            enrich(c,rows,p,order_key=sort_key,with_body=bool(body_format));rows.sort(key=sort_key)
            shape_messages(rows,compact=False,body_chars=cap)
            shape_messages([anchor],compact=False,body_chars=cap)
            decorate_messages(c,p,rows,include_system=include_system);decorate_messages(c,p,[anchor],include_system=include_system)
            for row in [*rows,anchor]:cap_display_text(row,cap)
        else:
            rows.sort(key=lambda r:(r['sent_at'],r['id']))
            enrich(c,rows,p,with_body=bool(body_format));enrich(c,[anchor],p,with_body=bool(body_format))
            shape_messages(rows,compact=compact,body_chars=cap)
            shape_messages([anchor],compact=compact,body_chars=cap)
        if body_format:
            # Response order: the anchor first; one rendering and one budget share per (message, revision).
            from .formatting_readback import attach
            attach([anchor,*rows])
        return rows,older,newer

    @staticmethod
    def _context_window(rows,message_id,explicit,older,newer,scans,before_count,after_count):
        window={'older_cursor':rows[0]['id'] if rows else message_id,
            'newer_cursor':rows[-1]['id'] if rows else message_id}
        if explicit:
            window.update({'before_count':len(older),'after_count':len(newer),
                'has_more_before':scans[0][1],'has_more_after':scans[1][1],
                'requested_before':before_count,'requested_after':after_count,
                'neighbors_coverage':'archived_messages_only'})
        return window

    @staticmethod
    def _context_conversation(c,p,anchor,rows,window,snapshot,include_system,conversation):
        from .conversation import presentation
        extra={}
        if conversation:
            from .conversation_state import conversation_state
            extra={'conversation_view':presentation(rows,order='asc',include_system=include_system),
                'conversation':conversation_state(c,p,anchor['account'],anchor['chat'])}
            from .message_queries import history_cursor
            # A hidden/tombstoned anchor still defines usable boundaries
            # when the requested visible window is empty.
            window.update(older_cursor=history_cursor(c,p,rows[0] if rows else anchor,order='desc',include_system=include_system,snapshot=snapshot),
                newer_cursor=history_cursor(c,p,rows[-1] if rows else anchor,order='asc',include_system=include_system,snapshot=snapshot),
                older_order='desc',newer_order='asc')
            window.update(view='conversation',include_system=include_system,
                anchor_hidden=bool(anchor['deleted'] or (not include_system and anchor['is_service'])))
        return extra

    def get_reviews(self, token, message_id, after='', limit=20):
        from .reviews import list_reviews
        with self.authorized(token,'get_reviews') as (c,p):
            self.message(c,p,message_id)
            return serial({**list_reviews(c,p,message_id,after,limit),**self.dataset(c,p)})

    def record_review(self, token, message_id, revision, result_ref, request_id, evidence=None):
        from .reviews import record
        with self.authorized(token,'record_review') as (c,p):
            return serial(record(c,p,message_id,revision,result_ref,request_id,evidence))

    def request_file_download(self, token, attachment_id):
        from .media_requests import request
        with self.authorized(token,'request_file_download') as (c,p):
            return request(self,c,p,attachment_id)

    def get_attachment(self, token, attachment_id):
        with self.authorized(token,'get_attachment') as (c, p):
            if not isinstance(attachment_id,str) or not 1<=len(attachment_id)<=256:raise APIError('not_found',404)
            where,args=scope_filter(p,'m')
            row = c.execute('''SELECT a.*,m.account,m.chat,m.provider_id AS provider_message_id,
                m.revision AS message_revision,ch.provider_chat AS provider_chat_id
                FROM attachments a JOIN messages m ON m.id=a.message
                JOIN chats ch ON ch.id=m.chat AND ch.account=m.account
                WHERE a.id=%s AND a.active AND NOT m.deleted AND '''+where,[attachment_id,*args]).fetchone()
            if not row:
                from .media_requests import occurrence
                pending=occurrence(c,p,attachment_id)
                if pending:
                    from .attachment_metadata import attachment_fields
                    fields=attachment_fields(pending)
                    targets(p,[pending['id']]);return serial({**fields,
                        'message_id':pending['message'],'reason':fields['download_reason'],
                        **self.dataset(c,p)})
                raise APIError('not_found', 404)
            from .file_index import _available,_absent
            from .attachment_metadata import attachment_fields,processing_phases
            from .file_lifecycle import lifecycle_lock,touch_original
            lifecycle_lock(c)
            row.update(processing_phases(c,row))
            if not _available(self.files,{'sha256':row['digest'],'bytes':row['bytes']}):
                if not _absent(self.files,{'sha256':row['digest']}):
                    row['original_residency']='missing'
                elif row.get('original_residency')=='evicted':
                    # A re-download is bound by the collector's history bound (ANYM-284).
                    from .media_requests import occurrence
                    job=occurrence(c,p,row['id'])
                    if job:row['download_eligibility']=job['download_eligibility']
                targets(p,[row['id']])
                return serial({**attachment_fields(row,original_present=False),
                    'status':'unavailable',**self.dataset(c,p)})
            touch_original(c,row['digest'],row['bytes'])
            grant = secrets.token_urlsafe(32)
            c.execute('DELETE FROM download_grants WHERE expires_at<now()')
            c.execute('''INSERT INTO download_grants VALUES (%s,%s,%s,%s,now()+interval '120 seconds')''',
                      (digest(grant), p['key_digest'], p['scope_version'], row['id']))
            targets(p,[row['id']])
            fields=attachment_fields(row,original_present=True)
            # Policy inputs (ANYM-805), not fields: attachment_fields has turned them into the reason it shows.
            row.pop('recognition_withheld',None);row.pop('recognition_reason',None)
            return {**serial(row),**serial(fields), 'status': 'ready', 'message_id':row['message'],
                    **self.dataset(c,p),
                    'download_url': f'{self.public_url}/v1/download/{grant}',
                    'download_expires_in': 120,
                    'notice': 'Temporary bearer link. Do not share or put it in public logs.'}

    def download(self, grant, *, active=None):
        """Resolve a bearer grant to (attachment row, original path, binding).

        The grant's TTL limits admission. `active` is the binding returned when
        a GET on this grant was admitted: revalidating that running transfer
        checks the current key, download permission, scope version and resource
        instead of the TTL, so it outlives the grant but not a revocation.
        """
        def resolve(c):
            if not isinstance(grant,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',grant):raise APIError('not_found',404)
            if active is None:
                row=c.execute('SELECT key_digest FROM download_grants WHERE digest=%s AND expires_at>clock_timestamp()',
                              (digest(grant),)).fetchone()
                if not row:raise APIError('not_found',404)
            p=self.identity_digest(c,row['key_digest'] if active is None else active[0],missing='not_found',status=404)
            if 'download' not in p['permissions']:
                error=APIError('not_found',404);error.audit_identity=p;raise error
            return p
        with self.access('download',resolve) as (c,p):
            from .file_lifecycle import lifecycle_lock,touch_original
            lifecycle_lock(c)
            where,args=scope_filter(p,'m')
            columns='''a.*,m.provider_id AS provider_message_id,
                (SELECT platform FROM accounts WHERE id=m.account) AS provider_platform,
                (SELECT metadata->>'media_kind' FROM message_metadata
                    WHERE message=m.id AND revision=m.revision) AS media_kind'''
            if active is None:
                row = c.execute('SELECT '+columns+''' FROM download_grants g
                    JOIN attachments a ON a.id=g.attachment JOIN messages m ON m.id=a.message
                    WHERE g.digest=%s AND g.expires_at>clock_timestamp() AND g.key_digest=%s
                    AND g.scope_version=%s AND a.active AND NOT m.deleted AND '''+where,
                    [digest(grant),p['key_digest'],p['scope_version'],*args]).fetchone()
            elif active[1]!=p['scope_version']:row=None
            else:
                # The expired grant row may already be deleted; its admitted
                # binding carries the same key, scope version and attachment.
                row = c.execute('SELECT '+columns+''' FROM attachments a JOIN messages m ON m.id=a.message
                    WHERE a.id=%s AND a.active AND NOT m.deleted AND '''+where,
                    [active[2],*args]).fetchone()
            if not row:
                raise APIError('not_found', 404)
            path = self.files/row['digest']
            if path.is_symlink() or not path.is_file() or path.stat().st_size != row['bytes']:
                raise APIError('attachment_unavailable', 503)
            touch_original(c,row['digest'],row['bytes'])
            targets(p,[row['id']])
            return row, path, (p['key_digest'],p['scope_version'],row['id'])

    def get_updates(self, token, consumer, limit=20, *, accounts=None,chats=None,senders=None,
                    origins=None,kinds=None,direction='',from_time='',to_time='',compact=False,
                    body_chars=None,include_coverage=True,exclude_channels=False,order='event',
                    reset_filters=False,expected_ack_version=None,start_from='beginning',view='events'):
        from .update_queries import get_updates
        with self.authorized(token,'get_updates') as (c,p):
            return get_updates(self,c,p,consumer,limit,accounts=accounts,chats=chats,senders=senders,
                origins=origins,kinds=kinds,direction=direction,from_time=from_time,to_time=to_time,
                compact=compact,body_chars=body_chars,include_coverage=include_coverage,
                exclude_channels=exclude_channels,order=order,reset_filters=reset_filters,
                expected_ack_version=expected_ack_version,start_from=start_from,view=view)

    def create_subscription(self, token, chat_ids=None, *, sources=None, sender_ids=None, kinds=None,
                            direction='incoming', origins=None, label='', expires_at=None, wake_rules=None, reply_to_send=None,
                            delivery=None):
        from .subscriptions import create_subscription
        with self.authorized(token, 'create_subscription') as (c, p):
            return create_subscription(self, c, p, chat_ids, sources=sources, sender_ids=sender_ids, kinds=kinds,
                direction=direction, origins=origins, label=label, expires_at=expires_at, wake_rules=wake_rules, reply_to_send=reply_to_send,
                delivery=delivery)

    def list_subscriptions(self, token, *, limit=50):
        from .subscriptions import list_subscriptions
        with self.authorized(token, 'list_subscriptions') as (c, p):
            return list_subscriptions(self, c, p, limit=limit)

    def get_subscription(self, token, subscription_id):
        from .subscriptions import get_subscription
        with self.authorized(token, 'get_subscription') as (c, p):
            return get_subscription(self, c, p, subscription_id)

    def update_subscription(self, token, subscription_id, version, *, action, resume_policy='catch_up'):
        from .subscriptions import update_subscription
        with self.authorized(token, 'update_subscription') as (c, p):
            return update_subscription(self, c, p, subscription_id, version,
                action=action, resume_policy=resume_policy)

    def get_subscription_events(self, token, subscription_id, *, after=0, limit=50, module_after=None):
        from .subscriptions import get_subscription_events
        with self.authorized(token, 'get_subscription_events') as (c, p):
            return get_subscription_events(self, c, p, subscription_id, after=after, limit=limit,
                module_after=module_after)

    def fetch_subscription_events(self, token, subscription_id, *, limit=50):
        from .subscription_consumption import fetch_subscription_events
        with self.authorized(token, 'fetch_subscription_events') as (c, p):
            return fetch_subscription_events(self, c, p, subscription_id, limit=limit)

    def ack_subscription_events(self, token, subscription_id, receipt, version):
        from .subscription_consumption import ack_subscription_events
        with self.authorized(token, 'ack_subscription_events') as (c, p):
            return ack_subscription_events(self, c, p, subscription_id, receipt, version)

    def configure_subscription_delivery(self, token, subscription_id, version, receiver_id=None, delivery_policy=None,
                                        endpoint_id=None):
        from .event_delivery import configure_subscription_delivery
        with self.authorized(token, 'configure_subscription_delivery') as (c, p):
            return configure_subscription_delivery(self, c, p, subscription_id, version, receiver_id, delivery_policy,
                endpoint_id)

    # ANYM-519: self-service endpoints. Each sync method returns at once; a staged test is pending until
    # the dispatcher sends it (delivery_endpoints.finish_inline_test waits for it).
    def endpoint_authorized(self, token, operation):
        # Before schema 49 (rolling upgrade) the audit CHECK has no endpoint operation: the denial is audited as
        # http_boundary (in schema 48's CHECK) for the caller, on REST and MCP alike, and answers delivery_disabled.
        from .delivery_endpoints import schema_ready
        with self.pool.connection() as c:
            ready = schema_ready(c)
        if not ready:
            with self.authorized(token) as (c, p):
                audit(c, p, 'http_boundary', 'denied', 'delivery_disabled')
            error = APIError('delivery_disabled', 409)
            error.operation, error.audited = operation, True
            raise error
        return self.authorized(token, operation)

    def register_delivery_endpoint(self, token, request_id, url, signing_secret, label='', headers=None, *, resolver=None):
        from .delivery_endpoints import register_delivery_endpoint
        with self.endpoint_authorized(token, 'register_delivery_endpoint') as (c, p):
            return register_delivery_endpoint(self, c, p, request_id, url, signing_secret, label, headers, resolver=resolver)

    def list_delivery_endpoints(self, token):
        from .delivery_endpoints import list_delivery_endpoints
        with self.endpoint_authorized(token, 'list_delivery_endpoints') as (c, p):
            return list_delivery_endpoints(self, c, p)

    def update_delivery_endpoint(self, token, endpoint_id, version, action, new_secret=None, headers=None, *,
                                 resume_test=None):
        # ANYM-520: a resume stages its signed test; the second call (resume_test) finishes it once delivered.
        from .delivery_endpoints import update_delivery_endpoint
        with self.endpoint_authorized(token, 'update_delivery_endpoint') as (c, p):
            return update_delivery_endpoint(self, c, p, endpoint_id, version, action, new_secret, headers,
                resume_test=resume_test)

    def list_delivery_attempts(self, token, endpoint_id, limit=50, before=None):
        from .delivery_endpoints import list_delivery_attempts
        with self.endpoint_authorized(token, 'list_delivery_attempts') as (c, p):
            return list_delivery_attempts(self, c, p, endpoint_id, limit, before)

    def delete_delivery_endpoint(self, token, endpoint_id, version):
        from .delivery_endpoints import delete_delivery_endpoint
        with self.endpoint_authorized(token, 'delete_delivery_endpoint') as (c, p):
            return delete_delivery_endpoint(self, c, p, endpoint_id, version)

    def test_delivery_endpoint(self, token, endpoint_id, request_id):
        from .delivery_endpoints import test_delivery_endpoint
        with self.endpoint_authorized(token, 'test_delivery_endpoint') as (c, p):
            return test_delivery_endpoint(self, c, p, endpoint_id, request_id)

    def delivery_endpoint_test(self, token, endpoint_id, request_id):
        # The inline-test poll: read only and unaudited, like the wait loop's reads.
        from .delivery_endpoints import endpoint_test
        with self.authorized(token) as (c, p):
            return endpoint_test(c, p, endpoint_id, request_id)

    def delivery_endpoint_signal(self, token, endpoint_id):
        # ANYM-583: health and last_error once the inline test finished; read only and unaudited like the poll.
        from .delivery_endpoints import endpoint_signal
        with self.authorized(token) as (c, p):
            return endpoint_signal(c, p, endpoint_id)

    def set_dynamic_subscriptions(self, enabled, *, verify_components=False):
        # Operator only (`set-dynamic-subscriptions`, ANYM-522); disabling is the rollback before older code runs.
        from .subscriptions import set_dynamic_subscriptions
        return set_dynamic_subscriptions(self, enabled, verify_components=verify_components)

    def set_source_filters(self, enabled, *, verify_components=False):
        # Operator only (`set-source-filters`, ANYM-825); disabling is the rollback before older code runs.
        from .subscriptions import set_source_filters
        return set_source_filters(self, enabled, verify_components=verify_components)

    def set_delivery_features(self, enabled, *, verify_components=False):
        # Operator only (`set-delivery-features`, ANYM-534); disabling cancels queued endpoint hints and tests.
        from .delivery_endpoints import set_delivery_features
        return set_delivery_features(self, enabled, verify_components=verify_components)

    def reload_delivery_policy(self):
        # Operator only, after editing DELIVERY_POLICY_FILE: waits for in-flight POSTs, cancels refused hosts' hints.
        from .delivery_endpoints import reload_delivery_policy
        return reload_delivery_policy(self)

    def list_delivery_endpoints_admin(self, principal=None):
        from .delivery_endpoints import list_endpoints_admin
        if principal is not None:principal_id(principal)
        return list_endpoints_admin(self, principal)

    def set_delivery_endpoint_operator_state(self, endpoint_id, enabled):
        from .delivery_endpoints import set_operator_state
        return set_operator_state(self, endpoint_id, enabled)

    def test_subscription(self, token, subscription_id, request_id):
        from .event_delivery import test_subscription
        with self.authorized(token, 'test_subscription') as (c, p):
            return test_subscription(self, c, p, subscription_id, request_id)

    def submit_feedback(self, token, request_id, category, description, *, operation=None, refs=None):
        from .feedback import submit_feedback
        with self.authorized(token, 'submit_feedback') as (c, p):
            return submit_feedback(self, c, p, request_id, category, description, operation=operation, refs=refs)

    def list_feedback(self, token, *, limit=50, status=None):
        from .feedback import list_feedback
        with self.authorized(token, 'list_feedback') as (c, p):
            return list_feedback(self, c, p, limit=limit, status=status)

    def export_feedback(self, **options):
        # Operator CLI only (ANYM-265): every principal's feedback, read-only; no MCP tool, REST route or gateway action.
        from .feedback import export_feedback
        with self.pool.connection() as c, c.transaction():
            return export_feedback(c, **options)

    def set_feedback_status(self, ids, status, *, fixed_in=None, spec_ref=None, duplicate_of=None, note=None,
                            unchanged_since=None, not_after=None):
        # Operator CLI only (ANYM-343): no operation permission, MCP tool, REST route or gateway action.
        from .feedback import set_feedback_status
        with self.pool.connection() as c:
            return set_feedback_status(c, ids, status, fixed_in=fixed_in, spec_ref=spec_ref,
                duplicate_of=duplicate_of, note=note, unchanged_since=unchanged_since, not_after=not_after)

    # Operator CLI only (ANYM-283): owner-made person links; no operation permission, MCP tool,
    # REST route or gateway action, and the gateway role has no write grant on person_links.
    def link_person(self, account, sender, person=None):
        from .person_links import link
        with self.pool.connection() as c:
            return link(c, account, sender, person)

    def unlink_person(self, account, sender):
        from .person_links import unlink
        with self.pool.connection() as c:
            return unlink(c, account, sender)

    def delete_person(self, person):
        from .person_links import delete
        with self.pool.connection() as c:
            return delete(c, person)

    def set_person_channel(self, person, purpose, account=None):
        from .person_links import prefer
        with self.pool.connection() as c:
            return prefer(c, person, purpose, account)

    def list_people(self, person=None):
        from .person_links import list_people
        with self.pool.connection() as c:
            return list_people(c, person)

    def ack_updates(self, token, consumer, receipt, expected_version):
        with self.authorized(token,'ack_updates') as (c, p):
            if not isinstance(expected_version,int) or isinstance(expected_version,bool) or expected_version<0:
                raise APIError('invalid_version')
            if not isinstance(receipt,str) or len(receipt)>128:raise APIError('invalid_receipt')
            if not isinstance(consumer,str) or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,64}',consumer):
                raise APIError('invalid_consumer')
            # Same lock order for GET and ACK; serialize concurrent ACKs of different receipts.
            state = c.execute('''SELECT * FROM consumers WHERE principal=%s AND name=%s AND scope_version=%s
                FOR UPDATE''', (p['id'], consumer, p['scope_version'])).fetchone()
            row = c.execute('''SELECT * FROM deliveries WHERE digest=%s AND principal=%s
                AND consumer=%s AND scope_version=%s AND expires_at>now()''',
                (digest(receipt), p['id'], consumer, p['scope_version'])).fetchone()
            if not state or not row or row['expected_version'] != expected_version:
                raise APIError('invalid_receipt', 409)
            if row['acked']:
                return {'acked': True, 'replayed': True, 'ack_version': state['version']}
            if state['version']!=expected_version or state['position']!=row['start_position']:
                raise APIError('stale_ack', 409)
            c.execute('''UPDATE consumers SET position=%s,version=version+1
                WHERE principal=%s AND name=%s AND scope_version=%s''',
                (row['end_position'], p['id'], consumer, p['scope_version']))
            c.execute('UPDATE deliveries SET acked=true WHERE digest=%s', (digest(receipt),))
            return {'acked': True, 'replayed': False, 'ack_version': expected_version+1}

    def release_retention_hold(self, subscription_ids=None):
        # Operator CLI only (ANYM-446): reviewed restored definitions become prunable again.
        from uuid import UUID
        ids = None if subscription_ids is None else sorted({str(UUID(value)) for value in subscription_ids})
        if ids is not None and not 1 <= len(ids) <= 1000:
            raise ValueError('invalid_subscription_ids')
        with self.pool.connection() as c:
            # ANYM-447: from schema 62 a closed definition's 30 days count from its release.
            closure = ',closed_at=clock_timestamp()' if schema_version(c) >= 62 else ''
            # A new closed_at restarts the 24-hour file protection of the named chats (ANYM-806): lock the rows
            # first, in id order, then the lifecycle lock last, as every subscription path does.
            held = [row['id'] for row in c.execute('''SELECT id FROM subscriptions
                WHERE retention_hold AND (%s::uuid[] IS NULL OR id=ANY(%s::uuid[])) ORDER BY id FOR UPDATE''',
                (ids, ids)).fetchall()]
            from .subscriptions import protection_lock
            protection_lock(c)
            released = c.execute('''UPDATE subscriptions SET retention_hold=false''' + closure + '''
                WHERE retention_hold AND id=ANY(%s::uuid[]) RETURNING id''', (held,)).fetchall()
        return {'released': len(released)}

    def set_subscription_retention(self, enabled, *, verify_components=False):
        # Operator CLI only (ANYM-446): enable after every serving reader reports retention_gap. From schema 62 it
        # also switches ANYM-447's deletion of finished hints and closed definitions; with verify_components (the
        # CLI) enabling refuses unless the API, matcher and dispatcher report this version on schema 62, so no
        # older reader serves hint counts without the deleted ones.
        from .subscriptions import TOMBSTONE_GATE, lock_retention
        value = 'enabled' if enabled else 'disabled'
        with self.pool.connection() as c:
            # Waits for in-flight pruning batches; every later batch reads this value.
            lock_retention(c, shared=False)
            version = int(c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value'])
            if version < 40:
                raise RuntimeError('subscription_retention_needs_schema_40')
            if enabled and verify_components and version >= 62:
                from .heartbeats import mismatch
                problem = mismatch(c)
                if problem is not None:
                    return problem
            for name in ('subscription_retention', *((TOMBSTONE_GATE,) if version >= 62 else ())):
                c.execute('''INSERT INTO probe_metadata(name,value) VALUES(%s,%s)
                    ON CONFLICT(name) DO UPDATE SET value=EXCLUDED.value''', (name, value))
        return {'subscription_retention': value}

    def migrate(self):
        with self.pool.connection() as c:
            c.execute('SELECT pg_advisory_xact_lock(74509137)')
            c.execute("SET LOCAL statement_timeout='180s'")
            # A lock wait would queue new writers behind migrate for up to 180 s; give up instead and
            # roll the whole migrate back.
            c.execute(MIGRATE_LOCK_TIMEOUT)
            if c.execute("SELECT to_regclass('public.probe_metadata') IS NOT NULL AS initialized").fetchone()['initialized']:
                lock_replay_tables(c)
            c.execute(Path(__file__).with_name('schema.sql').read_text())
            version=int(c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value'])
            if version>RUNTIME_SCHEMA: raise RuntimeError('database_newer_than_runtime')
            for target,name in [(2,'002_archive.sql'),(3,'003_collectors.sql'),(4,'004_whatsapp.sql'),(5,'005_access.sql'),(6,'006_reviews.sql'),(7,'007_search_indexes.sql'),(8,'008_people.sql'),(9,'009_file_index.sql'),(10,'010_search_audit.sql'),(11,'011_telegram_window.sql'),(12,'012_search.sql'),(13,'013_file_search.sql'),(14,'014_collector_health.sql'),(15,'015_enrichment.sql'),(16,'016_file_content.sql'),(17,'017_chat_evidence.sql'),(18,'018_collector_operations.sql'),(19,'019_whatsapp_identities.sql'),(20,'020_processing_admission.sql'),(21,'021_media_storage.sql'),(22,'022_outgoing.sql'),(23,'023_receipts.sql'),(24,'024_formatting.sql'),(25,'025_operations.sql'),(26,'026_subscriptions.sql'),(27,'027_reply_queue.sql'),(28,'028_send_reference.sql'),(29,'029_file_uploads.sql'),(30,'030_whatsapp_native_identities.sql'),(31,'031_media_requests.sql'),(32,'032_event_delivery.sql'),(33,'033_file_lifecycle.sql'),(34,'034_file_understanding.sql'),(35,'035_cold_archive.sql'),(36,'036_extend_key_audit.sql'),(37,'037_narrow_worker_roles.sql'),(38,'038_hint_policy.sql'),(39,'039_feedback_status.sql'),(40,'040_subscription_retention.sql'),(41,'041_reply_queue_controls.sql'),(42,'042_read_state_freshness.sql'),(43,'043_person_links.sql'),(44,'044_key_operation_id.sql'),(45,'045_message_purge.sql'),(46,'046_chat_roles.sql'),(47,'047_contact_search.sql'),(48,'048_dispatcher_heartbeat.sql'),(49,'049_delivery_endpoints.sql'),(50,'050_component_heartbeats.sql'),(51,'051_send_intents.sql'),(52,'052_host_admission_index.sql'),(53,'053_person_preferences.sql'),(54,'054_conflict_classes.sql'),(55,'055_failure_events.sql'),(56,'056_module_events.sql'),(57,'057_conflict_reevaluation.sql'),(58,'058_module_router.sql'),(59,'059_endpoint_refusal_indexes.sql'),(60,'060_dynamic_subscription_sources.sql'),(61,'061_delivery_attempts.sql'),(62,'062_subscription_tombstones.sql'),(63,'063_send_terminal_events.sql'),(64,'064_bot_press_permission.sql'),(65,'065_call_failed_provider_error.sql'),(66,'066_media_attempt_budget.sql'),(67,'067_recording_module_source.sql')]:
                migration=Path(__file__).with_name('migrations')/name
                sha=hashlib.sha256(migration.read_bytes()).hexdigest()
                if version<target:
                    # 040 resets lock_timeout to DEFAULT; each migration starts under the bound again.
                    c.execute(MIGRATE_LOCK_TIMEOUT)
                    c.execute(migration.read_text())
                    if target==17:
                        from .chat_evidence import backfill
                        backfill(c)
                    c.execute('INSERT INTO archive_migrations(version,sha256) VALUES(%s,%s)',(target,sha))
                    c.execute("UPDATE probe_metadata SET value=%s WHERE name='schema_version'",(str(target),))
                else:
                    applied=c.execute('SELECT sha256 FROM archive_migrations WHERE version=%s',(target,)).fetchone()
                    if not applied or applied['sha256']!=sha: raise RuntimeError('migration_checksum_mismatch')
            # Restores run without ACLs and reopen new functions to PUBLIC (ANYM-254).
            c.execute('''DO $$ BEGIN
                IF pg_has_role((SELECT proowner FROM pg_proc WHERE oid=to_regprocedure(
                    'public.publish_processing_event(text[],text,text,text,text,jsonb)')),'USAGE') THEN
                 REVOKE ALL ON FUNCTION public.publish_processing_event(text[],text,text,text,text,jsonb),
                  public.publish_file_text_event(text,text,text,text,jsonb),
                  public.publish_file_enrichment_event(text,text,text,text,jsonb) FROM PUBLIC;
                END IF;
                IF pg_has_role((SELECT proowner FROM pg_proc WHERE oid=to_regprocedure(
                    'public.subscription_hint_references(uuid,bigint,bigint,text[],text[])')),'USAGE') THEN
                 REVOKE ALL ON FUNCTION public.subscription_hint_references(uuid,bigint,bigint,text[],text[]) FROM PUBLIC;
                END IF;
                IF pg_has_role((SELECT proowner FROM pg_proc WHERE oid=to_regprocedure(
                    'public.journal_source_health(integer)')),'USAGE') THEN
                 REVOKE ALL ON FUNCTION public.journal_source_health(integer),
                  public.source_health_status(jsonb,text,timestamptz,integer) FROM PUBLIC;
                END IF;
                -- ANYM-562: the module journal's only writers and the matcher's hint gate.
                IF pg_has_role((SELECT proowner FROM pg_proc WHERE oid=to_regprocedure(
                    'public.publish_module_event(text,text,text,text,text,text,jsonb,text,text)')),'USAGE') THEN
                 REVOKE ALL ON FUNCTION public.publish_module_event(text,text,text,text,text,text,jsonb,text,text),
                  public.report_module_reachability(text,boolean),
                  public.module_event_valid(text,text,text,text,text,jsonb,text,text),
                  public.module_hints_ready(),public.subscription_module_fence() FROM PUBLIC;
                END IF;
                -- ANYM-699: only the gateway role (gateway_role.sql) wakes a media job; a restore must not open it.
                IF pg_has_role((SELECT proowner FROM pg_proc WHERE oid=to_regprocedure(
                    'public.wake_media_job(text)')),'USAGE') THEN
                 REVOKE ALL ON FUNCTION public.wake_media_job(text) FROM PUBLIC;
                END IF;
                -- Migration 033: queue_managed_original reopens a ready job for the gateway and cold-archive roles only.
                IF pg_has_role((SELECT proowner FROM pg_proc WHERE oid=to_regprocedure(
                    'public.queue_managed_original(text)')),'USAGE') THEN
                 REVOKE ALL ON FUNCTION public.queue_managed_original(text) FROM PUBLIC;
                END IF;
                -- Migration 033: the rest of the file-lifecycle functions (backup fences, residency, lifecycle locks).
                -- Each is guarded on its own owner; the constants below are the only text that reaches EXECUTE.
                DECLARE f text;
                BEGIN
                 FOREACH f IN ARRAY ARRAY['public.touch_original(text,bigint)','public.begin_backup_fence(text)',
                   'public.end_backup_fence(text)','public.register_original_residency()',
                   'public.register_resident_original(text,bigint)','public.lock_lifecycle_sources(text)'] LOOP
                  IF pg_has_role((SELECT proowner FROM pg_proc WHERE oid=to_regprocedure(f)),'USAGE') THEN
                   EXECUTE 'REVOKE ALL ON FUNCTION '||f||' FROM PUBLIC';
                  END IF;
                 END LOOP;
                END;
            END $$''')

    def expand_status(self):
        return expand_status(self.pool.conninfo)

    def migrate_expand(self, plan, *, root=None, before_commit=None):
        return migrate_expand(self.pool.conninfo,plan,root=root,before_commit=before_commit)

    def issue_key(self, principal, accounts, days=7, *, chats=UNSET, permissions=UNSET,
                  key_permissions=UNSET, token=None, operation_id=None, check=False, new_principal=False,
                  key_permissions_of=None):
        from . import key_admin
        return key_admin.issue_key(self, principal, accounts, days, chats=chats, permissions=permissions, key_permissions=key_permissions, token=token, operation_id=operation_id, check=check, new_principal=new_principal, key_permissions_of=key_permissions_of)

    def _issue_key(self,principal,accounts,days,chats,permissions,key_permissions,token,operation,check=False,
                   new_principal=False,key_of=None):
        from . import key_admin
        return key_admin._issue_key(self, principal, accounts, days, chats, permissions, key_permissions, token, operation, check, new_principal, key_of)

    def set_key_permissions(self,credential_id,permissions):
        from . import key_admin
        return key_admin.set_key_permissions(self, credential_id, permissions)

    @staticmethod
    def _write_policy(c,principal,old,accounts,chats,permissions,enabled):
        from . import key_admin
        return key_admin._write_policy(c, principal, old, accounts, chats, permissions, enabled)

    def set_access(self,principal,*,accounts=UNSET,chats=UNSET,permissions=UNSET,enabled=UNSET):
        from . import key_admin
        return key_admin.set_access(self, principal, accounts=accounts, chats=chats, permissions=permissions, enabled=enabled)

    def revoke(self, token=None, *, credential_id=None):
        from . import key_admin
        return key_admin.revoke(self, token, credential_id=credential_id)

    def extend_key(self,credential_id,days):
        from . import key_admin
        return key_admin.extend_key(self, credential_id, days)

    def list_keys(self,principal):
        principal_id(principal)
        with self.pool.connection() as c:
            return serial(c.execute('SELECT id,principal,expires_at,revoked,permissions,permission_version FROM api_keys WHERE principal=%s ORDER BY expires_at,id',
                                    (principal,)).fetchall())

    def list_all_keys(self):
        # ANYM-494: every key with public fields only; created_at is the operator's issue_key audit row, if any.
        with self.pool.connection() as c:
            return serial(c.execute('''SELECT k.id,k.principal,i.created_at,k.expires_at,k.revoked FROM api_keys k
                LEFT JOIN (SELECT key_id,min(occurred_at) AS created_at FROM audit_log
                    WHERE operation='issue_key' AND actor='operator' GROUP BY key_id) i ON i.key_id=k.id
                ORDER BY k.principal,k.expires_at,k.id''').fetchall())

    def key_info(self,tokens):
        # ANYM-494: a token's public record by its digest, in input order; the digest never leaves this method.
        if not isinstance(tokens,list) or not 1<=len(tokens)<=100:raise ValueError('invalid_key_count')
        result=[]
        with self.pool.connection() as c:
            for token in tokens:
                token=token.strip()
                if not 32<=len(token)<=256 or any(ch.isspace() for ch in token):
                    result.append({'found':False,'error':'invalid_key'});continue
                row=c.execute('SELECT id AS key_id,principal,expires_at,revoked FROM api_keys WHERE digest=%s',
                              (digest(token),)).fetchone()
                result.append(serial({'found':True,**row}) if row else {'found':False})
        return result

    def key_operation_info(self,operation_id):
        # ANYM-495: the key an operation issued, public fields only.
        operation_id=operation_name(operation_id)
        with self.pool.connection() as c:
            require_schema(c,KEY_OPERATION_SCHEMA)
            row=c.execute('''SELECT k.id AS key_id,k.principal,k.expires_at,k.revoked,o.operation_id
                FROM api_key_operations o JOIN api_keys k ON k.id=o.key_id WHERE o.operation_id=%s''',
                (operation_id,)).fetchone()
        return [serial({'found':True,**row}) if row else {'found':False,'operation_id':operation_id}]

    def principal_info(self,principal):
        # ANYM-496: a principal's scope and its keys' public fields, so the admin CLI can clone them; no digest.
        principal_id(principal)
        with self.pool.connection() as c:
            row=c.execute('SELECT id,accounts,chat_ids,permissions,enabled FROM principals WHERE id=%s',
                          (principal,)).fetchone()
            if not row:return {'found':False,'principal':principal}
            keys=c.execute('''SELECT id AS key_id,permissions,expires_at,revoked FROM api_keys WHERE principal=%s
                ORDER BY expires_at,id''',(principal,)).fetchall()
        for key in keys:
            # What the key may do (a NULL legacy ceiling included): a clone issues exactly this, explicitly.
            key['effective_permissions']=effective_permissions(row['permissions'],key['permissions'])
        return serial({'found':True,'principal':row['id'],'enabled':row['enabled'],'accounts':row['accounts'],
                       'chat_ids':row['chat_ids'],'permissions':row['permissions'],'keys':keys})

    def check_schema(self,version=KEY_OPERATION_SCHEMA):
        # ANYM-495 check mode: reads only the schema version, writes nothing.
        with self.pool.connection() as c:current=schema_version(c)
        return {'check':'ok' if current>=version else 'schema_not_ready','schema_version':current,'required':version}

    def audit_tail(self,principal='',limit=50):
        bounded(limit)
        if principal:principal_id(principal)
        with self.pool.connection() as c:
            return serial(c.execute('''SELECT * FROM audit_log WHERE (%s='' OR principal=%s)
                ORDER BY occurred_at DESC,id DESC LIMIT %s''',(principal,principal,limit)).fetchall())

    @staticmethod
    def event_snapshot(c, account, message, snapshot_metadata=None):
        row=c.execute('''SELECT id AS message,account,chat,sender,provider_id,body,revision,sent_at,deleted,direction
            FROM messages WHERE id=%s AND account=%s''',(message,account)).fetchone()
        if not row: raise ValueError('invalid_event_message')
        if snapshot_metadata is not None:
            if (not isinstance(snapshot_metadata,dict) or set(snapshot_metadata)-{'processing','reply_evidence'} or
                    len(json.dumps(snapshot_metadata,ensure_ascii=False).encode())>16384):
                raise ValueError('invalid_event_metadata')
            if 'reply_evidence' in snapshot_metadata:
                from .reply_evidence import validate as validate_reply_evidence
                validate_reply_evidence(snapshot_metadata['reply_evidence'])
            row.update(snapshot_metadata)
        from .result_evidence import formatting_evidence
        formatting_evidence(c,[row])
        return serial(row)

    @staticmethod
    def append_event(c, account, message, kind='message.created', origin='fixture', event_key=None,
                     snapshot_metadata=None, publisher=None):
        from psycopg.types.json import Jsonb
        row=Store.event_snapshot(c, account, message, snapshot_metadata)
        if publisher is not None and publisher not in ('publish_file_text_event','publish_file_enrichment_event'):
            raise ValueError('invalid_event_publisher')
        if publisher and c.execute(
                "SELECT coalesce(has_function_privilege(to_regprocedure(%s),'EXECUTE'),false) AS ok",
                ('public.'+publisher+'(text,text,text,text,jsonb)',)).fetchone()['ok']:
            # Schema 37 file workers: a fenced function re-checks the snapshot, kind and
            # stage, derives the origin and alone writes the journal and feed counter.
            # Until the reapplied template grants it (schema 36, or 37 with the previous
            # grants) the worker keeps the direct write those grants allow.
            return c.execute('SELECT public.'+publisher+'(%s,%s,%s,%s,%s) AS id',
                (account,message,kind,event_key,Jsonb(row))).fetchone()['id']
        value = c.execute('UPDATE feed_counter SET value=value+1 WHERE id=1 RETURNING value').fetchone()['value']
        c.execute('''INSERT INTO events(id,account,message,kind,revision,origin,snapshot,event_key)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s)''',
            (value,account,message,kind,row['revision'],origin,Jsonb(row),event_key))
        return value

    def seed(self):
        self.files.mkdir(mode=0o700, parents=True, exist_ok=True)
        content = b'PERSONAL OS SYNTHETIC PROBE\nFile integrity check: lighthouse-42\nNo personal data.\n'
        sha = hashlib.sha256(content).hexdigest()
        target = self.files/sha
        if not target.exists():
            temporary = self.files/('pending-'+secrets.token_hex(8))
            fd = os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
            with os.fdopen(fd,'wb') as f:
                f.write(content); f.flush(); os.fsync(f.fileno())
            os.replace(temporary,target)
            fd = os.open(self.files, os.O_RDONLY)
            try: os.fsync(fd)
            finally: os.close(fd)
        if hashlib.sha256(target.read_bytes()).hexdigest()!=sha:
            raise ValueError('Fixture file corrupt')
        with self.pool.connection() as c:
            c.execute('SELECT value FROM feed_counter WHERE id=1 FOR UPDATE')
            for aid, platform in [('telegram-demo','telegram'),('whatsapp-demo-1','whatsapp'),('whatsapp-demo-2','whatsapp')]:
                c.execute('INSERT INTO accounts(id,platform,label) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING', (aid,platform,'Synthetic '+aid))
                c.execute('INSERT INTO chats VALUES(%s,%s,%s) ON CONFLICT DO NOTHING', (aid+'-chat',aid,'Synthetic chat'))
            for mid, aid, text, sender, day in [
                ('demo-tg-1','telegram-demo','Тестовый проект Маяк: встреча во вторник. Это вымышленные данные.','demo-person-1',1),
                ('demo-tg-2','telegram-demo','Файл lighthouse-42 приложен для проверки скачивания.','demo-person-2',2),
                ('demo-wa-1','whatsapp-demo-1','Synthetic project Atlas: budget review and planning.','demo-person-1',3),
                ('demo-private-1','whatsapp-demo-2','PRIVATE_SCOPE_SENTINEL synthetic hidden message.','demo-person-3',4)]:
                inserted = c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id''',
                    (mid,aid,aid+'-chat',mid,sender,text,datetime(2026,10,day,12,tzinfo=timezone.utc))).fetchone()
                if inserted:
                    eid=self.append_event(c, aid, mid)
                    c.execute('''INSERT INTO message_revisions(message,revision,snapshot)
                        SELECT message,revision,snapshot FROM events WHERE id=%s''',(eid,))
            c.execute('INSERT INTO attachments(id,message,digest,filename,mime,bytes) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                      ('demo-file-1','demo-tg-2',sha,'probe-note.txt','text/plain',len(content)))
