"""Operator CLI: administration remains local/SSH-only, not an MCP tool."""
import argparse
import json
import os
from pathlib import Path

from .store import Store
from .policy import PERMISSIONS, UNSET
from .messenger_operations import OPERATIONS


def main():
    from .telemetry import configure
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['serve','init','migrate','issue-key','revoke-key',
        'replay','snapshot','verify-restore','quarantine-restore','register-account',
        'set-access','list-keys','audit-tail','set-sending','set-key-permissions',
        'set-messenger-operations','extend-key','set-feedback-status','export-feedback','release-retention-hold',
        'set-subscription-retention','link-person','unlink-person','delete-person','list-people'])
    parser.add_argument('--principal')
    parser.add_argument('--accounts',nargs='+')
    parser.add_argument('--key-file',type=Path)
    parser.add_argument('--key-id',help='Public credential UUID; never the token or its hash')
    group=parser.add_mutually_exclusive_group()
    group.add_argument('--chats',nargs='*',default=UNSET)
    group.add_argument('--all-chats',action='store_true')
    parser.add_argument('--permissions',nargs='*',default=UNSET,choices=sorted(PERMISSIONS))
    parser.add_argument('--key-permissions',nargs='*',default=UNSET,choices=sorted(PERMISSIONS),
        help='issue-key: independently narrow this key within the principal permission ceiling')
    parser.add_argument('--operations',nargs='*',default=UNSET,choices=sorted(OPERATIONS),
        help='set-messenger-operations: explicit account opt-in; an empty list disables all')
    state=parser.add_mutually_exclusive_group()
    state.add_argument('--enable',action='store_true')
    state.add_argument('--disable',action='store_true')
    parser.add_argument('--limit',type=int,default=50)
    parser.add_argument('--days',type=int,help='issue-key: 1-30 (default 7); extend-key: required, 1-183')
    parser.add_argument('--input',type=Path)
    parser.add_argument('--spool',type=Path)
    parser.add_argument('--snapshot-dir',type=Path)
    parser.add_argument('--dump-command',help='Operator-only JSON argv; never a shell string')
    parser.add_argument('--recovery-config',type=Path,help='Private operator JSON: quiesced sessions/settings/spools')
    parser.add_argument('--account')
    parser.add_argument('--platform',choices=['telegram','whatsapp'])
    parser.add_argument('--label')
    parser.add_argument('--include-chats',nargs='*',default=[])
    parser.add_argument('--exclude-chats',nargs='*',default=[])
    parser.add_argument('--discover-chats',action='store_true')
    parser.add_argument('--synthetic',action='store_true')
    parser.add_argument('--id',nargs='+',dest='feedback_ids',help='set-feedback-status: 1-100 feedback UUIDs')
    parser.add_argument('--status',help='set-feedback-status: received, accepted, declined, duplicate, fixed or verify_requested')
    parser.add_argument('--fixed-in',help='set-feedback-status: deedkit@<40-hex SHA> or anymsg@<semver>')
    parser.add_argument('--spec',help='set-feedback-status: ANYM-<n>')
    parser.add_argument('--duplicate-of',help='set-feedback-status: a feedback UUID of the same key')
    parser.add_argument('--note',help='set-feedback-status: at most 280 characters, no control characters')
    parser.add_argument('--unchanged-since',help='set-feedback-status: ISO 8601 time with an offset; change nothing if any item changed after it')
    parser.add_argument('--not-after',help='set-feedback-status: ISO 8601 time with an offset; change nothing once the database clock is past it')
    parser.add_argument('--since',help='export-feedback: ISO 8601 time with an offset; submissions created since then')
    parser.add_argument('--changed-since',help='export-feedback: ISO 8601 time with an offset; dispositions changed since then')
    parser.add_argument('--page',help='export-feedback: next_page of the same export')
    parser.add_argument('--overlap',type=int,help='export-feedback: minutes before --since/--changed-since, 2-1440 (default 10)')
    parser.add_argument('--subscription-ids',nargs='+',help='release-retention-hold: subscription UUIDs')
    parser.add_argument('--all-subscriptions',action='store_true',help='release-retention-hold: every held subscription')
    parser.add_argument('--sender',help='link-person/unlink-person: exact provider sender ID (sender_id from search_people)')
    parser.add_argument('--person',help='link-person: add to this person UUID (default: a new person); delete-person, list-people')
    args=parser.parse_args()
    if args.command == 'serve':
        configure('gateway')
    dsn=Path(os.environ['DATABASE_URL_FILE']).read_text().strip()
    public_url=os.environ.get('PROBE_PUBLIC_URL','http://127.0.0.1:18940')
    store=Store(dsn,Path(os.environ.get('PROBE_FILES','./probe-files')),public_url)
    try:
        if args.command=='init':
            store.migrate(); store.seed()
            print(json.dumps({'initialized':True,'synthetic':True}))
        elif args.command=='migrate':
            store.migrate()
            with store.pool.connection() as c:
                version=int(c.execute("SELECT value FROM probe_metadata WHERE name='schema_version'").fetchone()['value'])
            print(json.dumps({'schema_version':version}))
        elif args.command=='register-account':
            if not args.account or not args.platform or not args.label:
                parser.error('register-account requires --account, --platform and --label')
            from .archive import Archive
            Archive(store).register(args.account,args.platform,args.label,synthetic=args.synthetic,
                include_chats=args.include_chats,exclude_chats=args.exclude_chats,discover=args.discover_chats)
            print(json.dumps({'registered':True,'account':args.account,'provider_connected':False}))
        elif args.command=='set-sending':
            if not args.account or not (args.enable or args.disable):
                parser.error('set-sending requires --account and --enable or --disable')
            from .archive import Archive
            Archive(store).set_sending(args.account,args.enable)
            print(json.dumps({'account':args.account,'send_enabled':args.enable}))
        elif args.command=='set-messenger-operations':
            if not args.account or args.operations is UNSET:
                parser.error('set-messenger-operations requires --account and explicit --operations')
            store.set_messenger_operations(args.account,args.operations)
            print(json.dumps({'account':args.account,'operations_enabled':args.operations}))
        elif args.command=='replay':
            if not args.input or not args.spool: parser.error('replay requires --input and --spool')
            from .archive import Archive
            from .spool import DurableSpool
            from .envelope import MAX_ENVELOPE
            queue=DurableSpool(args.spool); archive=Archive(store); queued=0
            with args.input.open('rb') as source:
                while line:=source.readline(MAX_ENVELOPE+2):
                    if len(line)>MAX_ENVELOPE+1: raise ValueError('envelope_too_large')
                    queue.put(json.loads(line)); queued+=1
                    queue.drain(archive.ingest,limit=100)
            drained=queue.drain(archive.ingest)
            print(json.dumps({'queued':queued,'final_drain':drained}))
        elif args.command in ('snapshot','verify-restore','quarantine-restore'):
            from .backup import create_snapshot,verify_snapshot,quarantine_restore
            if args.command=='quarantine-restore':
                quarantine_restore(store); print(json.dumps({'collectors_disabled':True,'api_keys_revoked':True}))
            else:
                if not args.snapshot_dir: parser.error('requires --snapshot-dir')
                if args.command=='snapshot':
                    if not args.dump_command: parser.error('snapshot requires --dump-command')
                    recovery=None
                    if args.recovery_config:
                        from .telegram_collector import private_json
                        recovery=private_json(args.recovery_config)
                    result=create_snapshot(store,args.snapshot_dir,json.loads(args.dump_command),recovery)
                else: result=verify_snapshot(store,args.snapshot_dir)
                print(json.dumps(result))
        elif args.command=='issue-key':
            if not args.principal or not args.accounts or not args.key_file:
                parser.error('issue-key requires --principal, --accounts and --key-file')
            # Reserve the output first; never silently replace a user's active credential.
            fd=os.open(args.key_file,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
            with os.fdopen(fd,'w') as out:
                out.write(store.issue_key(args.principal,args.accounts,7 if args.days is None else args.days,
                    chats=None if args.all_chats else args.chats,permissions=args.permissions,
                    key_permissions=args.key_permissions)+'\n')
            print(json.dumps({'issued':True,'principal':args.principal,'key_file':str(args.key_file)}))
        elif args.command=='revoke-key':
            if bool(args.key_file)==bool(args.key_id):parser.error('revoke-key requires exactly one of --key-file or --key-id')
            count=store.revoke(args.key_file.read_text().strip() if args.key_file else None,credential_id=args.key_id)
            print(json.dumps({'revoked':count}))
        elif args.command=='extend-key':
            if not args.key_id or args.key_file or args.days is None:
                parser.error('extend-key requires --key-id and --days, never --key-file')
            print(json.dumps(store.extend_key(args.key_id,args.days)))
        elif args.command in ('link-person','unlink-person'):
            # ANYM-283: owner-made links only; an agent key has no path to these.
            if not args.account or not args.sender:
                parser.error(args.command+' requires --account and --sender')
            print(json.dumps(store.link_person(args.account,args.sender,args.person) if args.command=='link-person'
                else store.unlink_person(args.account,args.sender)))
        elif args.command=='delete-person':
            if not args.person: parser.error('delete-person requires --person')
            print(json.dumps(store.delete_person(args.person)))
        elif args.command=='list-people':
            print(json.dumps(store.list_people(args.person)))
        elif args.command=='set-feedback-status':
            if not args.feedback_ids or not args.status:
                parser.error('set-feedback-status requires --id and --status')
            print(json.dumps(store.set_feedback_status(args.feedback_ids,args.status,fixed_in=args.fixed_in,
                spec_ref=args.spec,duplicate_of=args.duplicate_of,note=args.note,
                **{name:value for name,value in (('unchanged_since',args.unchanged_since),('not_after',args.not_after)) if value})))
        elif args.command=='export-feedback':
            print(json.dumps(store.export_feedback(since=args.since,changed_since=args.changed_since,page=args.page,
                overlap=args.overlap,limit=args.limit)))
        elif args.command=='release-retention-hold':
            if bool(args.subscription_ids)==args.all_subscriptions:
                parser.error('release-retention-hold requires exactly one of --subscription-ids or --all-subscriptions')
            print(json.dumps(store.release_retention_hold(args.subscription_ids)))
        elif args.command=='set-subscription-retention':
            if not (args.enable or args.disable):
                parser.error('set-subscription-retention requires --enable or --disable')
            print(json.dumps(store.set_subscription_retention(args.enable)))
        elif args.command=='set-access':
            if not args.principal:parser.error('set-access requires --principal')
            result=store.set_access(args.principal,accounts=args.accounts if args.accounts is not None else UNSET,
                chats=None if args.all_chats else args.chats,permissions=args.permissions,
                enabled=True if args.enable else (False if args.disable else UNSET))
            print(json.dumps(result))
        elif args.command=='set-key-permissions':
            if not args.key_id or args.permissions is UNSET:
                parser.error('set-key-permissions requires --key-id and explicit --permissions')
            print(json.dumps(store.set_key_permissions(args.key_id,args.permissions)))
        elif args.command=='list-keys':
            if not args.principal:parser.error('list-keys requires --principal')
            print(json.dumps(store.list_keys(args.principal)))
        elif args.command=='audit-tail':
            print(json.dumps(store.audit_tail(args.principal or '',args.limit)))
        else:
            import uvicorn
            from .app import create_app
            hosts=os.environ.get('PROBE_ALLOWED_HOSTS','127.0.0.1:18940,localhost:18940').split(',')
            uvicorn.run(create_app(store,hosts),host=os.environ.get('PROBE_BIND','127.0.0.1'),
                        port=int(os.environ.get('PROBE_PORT','18940')),access_log=False,
                        proxy_headers=False,log_level='warning',limit_concurrency=32)
    finally: store.close()


if __name__=='__main__': main()
