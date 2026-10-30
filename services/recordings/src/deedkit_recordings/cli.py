import argparse
import logging


def main():
    parser = argparse.ArgumentParser(description='Private recordings backend; no provider login or activation')
    parser.add_argument('--config', required=True, help='Absolute path to a private owner-installed configuration')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', default=13234, type=int)
    parser.add_argument('--backup-to', help='Create a private, consistent text archive snapshot at a new absolute path')
    args = parser.parse_args()
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
    import uvicorn
    from .app import create_app
    # Provider URLs may carry source IDs/cursors. No request URL/body logging.
    for name in ('httpx', 'httpcore'):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    uvicorn.run(create_app(args.config), host=args.host, port=args.port, workers=1, access_log=False,
                limit_concurrency=64, timeout_keep_alive=5)


if __name__ == '__main__':
    main()
