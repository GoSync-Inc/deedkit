import argparse
import json
import logging
import os


def operate(parser, args):
    """Owner-requested deletion and tombstone lifting (ANYM-730): an operator command, never an HTTP route or an agent tool.
    Preview unless `--yes`; the service must be stopped (the single-writer lock). The receipt has counts and identity only."""
    from .archive_store import Store, namespace
    from .security import Failure, read_document
    if args.delete and args.yes and not args.snapshot_to:
        parser.error('--delete --yes needs --snapshot-to: the post-deletion snapshot is part of the operation')
    try:
        doc, _ = read_document(args.config)
        if doc.archive is None:
            raise Failure('archive_disabled')
        by_id = {account.id: account for account in doc.accounts}
        if args.account is not None and args.account not in by_id:
            raise Failure('deletion_selector_invalid')
        # SQLite's VACUUM temporary file stays in the private archive directory (the one-off container may have no /tmp).
        os.environ['SQLITE_TMPDIR'] = str(doc.archive.directory)
        store = Store(doc.archive)
        try:
            if args.lift_tombstone:
                if args.account is None or not args.provider_id:
                    raise Failure('deletion_selector_required')
                receipt = store.lift_tombstone(namespace(by_id[args.account]), args.provider_id, apply=args.yes)
            else:
                receipt = store.delete_records(
                    refs=args.ref or None, namespaces=[namespace(by_id[args.account])] if args.account else None,
                    created_from=args.created_from, created_to=args.created_to, apply=args.yes, snapshot_to=args.snapshot_to)
        finally:
            store.close()
    except Failure as error:
        parser.exit(1, 'Archive deletion failed: ' + error.code + '\n')
    print(json.dumps(receipt, sort_keys=True))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Private recordings backend; no provider login or activation')
    parser.add_argument('--config', required=True, help='Absolute path to a private owner-installed configuration')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', default=13234, type=int)
    parser.add_argument('--backup-to', help='Create a private, consistent text archive snapshot at a new absolute path')
    parser.add_argument('--delete', action='store_true', help='Owner-requested deletion: preview, or apply with --yes (service stopped)')
    parser.add_argument('--ref', action='append', help='A recording_ref to delete (repeatable); alone, never with an account range')
    parser.add_argument('--account', help='Account id: with --created-from/--created-to, or with --lift-tombstone')
    parser.add_argument('--created-from', help='Delete recordings created at or after this time (ISO 8601)')
    parser.add_argument('--created-to', help='Delete recordings created at or before this time (ISO 8601)')
    parser.add_argument('--yes', action='store_true', help='Apply --delete or --lift-tombstone (without it: preview only)')
    parser.add_argument('--snapshot-to', help='With --delete --yes (required): write the post-deletion snapshot at a new absolute path')
    parser.add_argument('--lift-tombstone', action='store_true', help='Allow one deleted recording to be archived again (needs --account and --provider-id)')
    parser.add_argument('--provider-id', help='The provider recording id whose tombstone to lift')
    args = parser.parse_args(argv)
    if args.delete or args.lift_tombstone:
        if args.backup_to or (args.delete and args.lift_tombstone):
            parser.error('--delete, --lift-tombstone and --backup-to are separate commands')
        return operate(parser, args)
    if args.backup_to:
        from .archive_store import backup_archive
        from .security import Failure, read_document
        try:
            doc, _ = read_document(args.config)
            if doc.archive is None:
                raise Failure('archive_disabled')
            backup_archive(doc.archive, args.backup_to)
        except Failure as error:
            parser.exit(1, 'Archive backup failed: ' + error.code + '\n')
        print('Archive backup saved.')
        return
    # Fail closed before anything binds: with RECORDINGS_EGRESS_ADMISSION=required the host egress rules must be in force.
    from .admission import require
    require()
    import uvicorn
    from .app import create_app
    # Provider URLs may carry source IDs/cursors. No request URL/body logging.
    for name in ('httpx', 'httpcore'):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    uvicorn.run(create_app(args.config), host=args.host, port=args.port, workers=1, access_log=False,
                limit_concurrency=64, timeout_keep_alive=5)


if __name__ == '__main__':
    main()
