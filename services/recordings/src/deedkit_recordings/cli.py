import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import stat


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


def private_target(path):
    """A new file in an existing directory that only its owner can enter; the refusal names no value."""
    target = Path(path)
    if not target.is_absolute():
        raise ValueError('absolute_path_required')
    parent = os.lstat(target.parent)
    if not stat.S_ISDIR(parent.st_mode) or parent.st_mode & 0o077 or parent.st_uid != os.geteuid():
        raise ValueError('directory_not_private')
    if os.path.lexists(target):
        raise ValueError('file_exists')
    return target


def write_private(target, text):
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(text + '\n')


def issue_hint(parser, args):
    """ANYM-760: make the Omi webhook's path token and channel credential. Each goes only into its private file; the output is
    their digests for the configuration. Run it once per rotation with new file names: nothing old changes until removed."""
    origin = args.public_origin or ''
    if not re.fullmatch(r'https://[A-Za-z0-9.-]{1,253}(:[0-9]{1,5})?', origin) or not args.url_file or not args.channel_key_file:
        parser.exit(1, 'Omi hint issue refused: invalid_arguments\n')
    try:
        if os.path.abspath(args.url_file) == os.path.abspath(args.channel_key_file):
            raise ValueError('same_file')
        url_file, key_file = private_target(args.url_file), private_target(args.channel_key_file)
        token, key = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        write_private(url_file, origin + '/omi/memory/' + token)
        try:
            write_private(key_file, key)
        except OSError:
            os.unlink(url_file)
            raise
    except ValueError as error:
        parser.exit(1, f'Omi hint issue refused: {error}\n')
    except OSError:
        parser.exit(1, 'Omi hint issue refused: file_unavailable\n')
    print(json.dumps({'channel_key_sha256': hashlib.sha256(key.encode()).hexdigest(),
                      'path_token_sha256': hashlib.sha256(token.encode()).hexdigest()}, sort_keys=True))


def run_receiver(parser, args):
    """ANYM-760: the internet-facing receiver, the same image with another command. No archive, no provider, no admission probe.
    With `--unix` (the deployed form) it serves on `hint.RECEIVER_SOCKET` only, in a container without a network. The paths are
    constants, not arguments: the promotion tool refuses a mounted path in a command."""
    import uvicorn
    from . import hint
    from .hint import CONNECTION_LIFETIME, RECEIVER_CONNECTIONS, Receiver, bounded_protocol, unix_socket
    for name in ('httpx', 'httpcore'):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    # Secret path: no request line is logged, and the proxy's access log carries none either.
    options = dict(workers=1, access_log=False, lifespan='off', log_level='warning', server_header=False, date_header=False,
                   proxy_headers=False, limit_concurrency=RECEIVER_CONNECTIONS, timeout_keep_alive=1,
                   http=bounded_protocol(RECEIVER_CONNECTIONS, CONNECTION_LIFETIME), h11_max_incomplete_event_size=16384)
    if not args.unix:
        uvicorn.run(Receiver(args.config), host=args.host, port=args.port, **options)
        return
    try:
        listening = unix_socket(hint.RECEIVER_SOCKET)
    except (OSError, ValueError):
        parser.exit(1, 'Omi hint receiver refused: socket_unavailable\n')
    uvicorn.Server(uvicorn.Config(Receiver(args.config), **options)).run(sockets=[listening])


def run_relay(parser, args):
    """ANYM-760: the byte relay between the proxy and the receiver's socket. No configuration, no secret, nothing parsed."""
    from . import hint
    hint.run_relay(args.host, args.port, hint.RECEIVER_SOCKET)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Private recordings backend; no provider login or activation')
    parser.add_argument('--config', help='Absolute path to a private owner-installed configuration (required but for --omi-hint-issue and --omi-hint-relay)')
    parser.add_argument('--omi-hint-receiver', action='store_true', help='Run the Omi webhook receiver: --config names its own private file')
    parser.add_argument('--omi-hint-issue', action='store_true', help='Make a webhook path token and channel credential (private files only; prints digests)')
    parser.add_argument('--public-origin', help='With --omi-hint-issue: the https origin of the existing proxy')
    parser.add_argument('--url-file', help='With --omi-hint-issue: a new file for the full webhook URL, in a 0700 directory')
    parser.add_argument('--channel-key-file', help="With --omi-hint-issue: a new file for the channel credential, in the receiver's 0700 directory")
    parser.add_argument('--unix', action='store_true', help="With --omi-hint-receiver: serve on the receiver's Unix socket only, not --host/--port")
    parser.add_argument('--omi-hint-relay', action='store_true', help="Pipe TCP connections on --host/--port to the receiver's Unix socket")
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
    if args.omi_hint_issue:
        return issue_hint(parser, args)
    if args.omi_hint_relay:
        return run_relay(parser, args)
    if not args.config:
        parser.error('the following arguments are required: --config')
    if args.omi_hint_receiver:
        return run_receiver(parser, args)
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
