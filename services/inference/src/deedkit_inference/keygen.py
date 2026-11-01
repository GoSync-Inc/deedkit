#!/usr/bin/env python3
"""Key issue and rotation for the inference service's policy file (ANYM-684 criterion 6).

Stdlib only. It runs on the mini as the service user. The policy is the file of the ANYM-682 README: `version` 1,
a `revision` that only grows, principals with one or two `{sha256, expires_at}` keys. This tool writes only the
sha256 of a key; the key itself goes once into a new 0600 file (O_EXCL) and is never printed. Writers take the
same lock file as the service's `write_policy` (`<policy>.lock`), replace the policy atomically with a 0600 file
and raise `revision` by one.

    keygen --principal ID --scopes audio,image --max-jobs-per-day N --max-concurrent N --expires-days N --key-file F
    keygen --rotate --principal ID --expires-days N --key-file F          # adds a second key (old stays valid)
    keygen --confirm-readback "<inference_probe.py line>" --principal ID  # the worker's own read-back succeeded
    keygen --retire-old --principal ID                                    # removes the old hash, bumps revision

`--retire-old` is refused until `--confirm-readback` has accepted a line that proves the NEW key answered
(`reachable=yes` and `key_expires=` equal to the new key's expiry, which differs from the old key's): a copy that
failed leaves the old key working and nothing is paused. The rotation record `<policy>.rotation` holds hashes only.
"""
from __future__ import annotations

from collections.abc import Generator
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile
import time

DEFAULT_POLICY = '/Users/_deedkitinf/policy.json'
SCOPES = ('audio', 'image', 'document')
MAX_POLICY_BYTES = 64 * 1024
ISO = '%Y-%m-%dT%H:%M:%SZ'
PRINCIPAL = re.compile(r'^[a-z0-9][a-z0-9._-]{0,63}$')
utcnow = time.time  # tests replace it


class KeygenError(Exception):
    pass


def iso(epoch: float) -> str:
    return time.strftime(ISO, time.gmtime(epoch))


@contextlib.contextmanager
def policy_lock(policy: str | Path) -> Generator[None]:
    """The lock file ANYM-682's `write_policy` takes: one writer at a time."""
    descriptor = os.open(f'{policy}.lock', os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)  # closing releases the flock


def load_policy(path: Path) -> dict:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return {'version': 1, 'revision': 0, 'principals': []}
    if not stat.S_ISREG(info.st_mode):
        raise KeygenError('the policy is not a regular file (a symlink is refused)')
    if info.st_mode & 0o077:
        raise KeygenError('the policy has group or other permission bits; it must be 0600')
    if info.st_size > MAX_POLICY_BYTES:
        raise KeygenError('the policy is larger than 64 KiB')
    document = json.loads(path.read_text())
    if document.get('version') != 1 or not isinstance(document.get('revision'), int) or not isinstance(document.get('principals'), list):
        raise KeygenError('the policy does not have the version 1 shape')
    return document


def atomic_write(path: Path, content: str) -> None:
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f'.{path.name}.')
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'w') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def save_policy(path: Path, document: dict) -> None:
    document['revision'] += 1  # only ever grows
    atomic_write(path, json.dumps(document, indent=2) + '\n')


def load_rotations(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return {}


def save_rotations(path: Path, rotations: dict) -> None:
    atomic_write(path, json.dumps(rotations, indent=2) + '\n')


def new_key_file(path: Path) -> str:
    """Creates `path` (0600, never over an existing file), writes a fresh key into it once, returns the key."""
    key = 'dki_' + secrets.token_urlsafe(32)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'w') as handle:
            handle.write(key + '\n')
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise
    return key


def issue(policy: Path, document: dict, entry_for: dict, key_file: Path, update) -> None:
    """Writes the key file, then the policy; a failed policy write removes the key file again."""
    new_key_file(key_file)
    key = key_file.read_text().strip()
    entry_for['sha256'] = hashlib.sha256(key.encode()).hexdigest()
    try:
        update()
        save_policy(policy, document)
    except BaseException:
        with contextlib.suppress(OSError):
            key_file.unlink()
        raise


def find(document: dict, principal: str) -> dict | None:
    return next((item for item in document['principals'] if item['id'] == principal), None)


def create(args, policy: Path) -> None:
    for scope in args.scopes.split(','):
        if scope not in SCOPES:
            raise KeygenError(f'scope {scope!r} is not one of {", ".join(SCOPES)}')
    expires_at = iso(utcnow() + args.expires_days * 86400)
    with policy_lock(policy):
        document = load_policy(policy)
        if find(document, args.principal):
            raise KeygenError(f'principal {args.principal} exists; use --rotate')
        entry = {'sha256': '', 'expires_at': expires_at}
        principal = {'id': args.principal, 'keys': [entry], 'scopes': args.scopes.split(','),
                     'max_jobs_per_day': args.max_jobs_per_day, 'max_concurrent': args.max_concurrent}
        issue(policy, document, entry, Path(args.key_file), lambda: document['principals'].append(principal))
    print(f'principal {args.principal}: key written to {args.key_file} (mode 0600, shown nowhere else); '
          f'expires_at {expires_at}; policy revision {document["revision"]}')


def rotate(args, policy: Path) -> None:
    rotations_path = Path(f'{policy}.rotation')
    expires_at = iso(utcnow() + args.expires_days * 86400)
    with policy_lock(policy):
        document = load_policy(policy)
        principal = find(document, args.principal)
        rotations = load_rotations(rotations_path)
        if principal is None:
            raise KeygenError(f'principal {args.principal} does not exist')
        if len(principal['keys']) != 1 or args.principal in rotations:
            raise KeygenError('a rotation is already under way (two keys); confirm and retire the old one first')
        old = principal['keys'][0]
        if expires_at <= old['expires_at']:
            raise KeygenError(f'the new key must expire after the old one ({old["expires_at"]}): the read-back tells them apart by it')
        entry = {'sha256': '', 'expires_at': expires_at}

        def update() -> None:
            principal['keys'].append(entry)
            rotations[args.principal] = {'state': 'issued', 'old_sha256': old['sha256'], 'new_sha256': entry['sha256'],
                                         'old_expires_at': old['expires_at'], 'new_expires_at': expires_at}
            save_rotations(rotations_path, rotations)  # before the policy: a failed policy write removes it again

        try:
            issue(policy, document, entry, Path(args.key_file), update)
        except BaseException:
            if rotations.pop(args.principal, None) is not None:
                with contextlib.suppress(OSError):
                    save_rotations(rotations_path, rotations)
            raise
    print(f'principal {args.principal}: second key written to {args.key_file} (mode 0600); expires_at {expires_at}; '
          f'both keys valid; policy revision {document["revision"]}. Deliver it, then run the worker read-back.')


def confirm(args, policy: Path) -> None:
    rotations_path = Path(f'{policy}.rotation')
    with policy_lock(policy):
        rotations = load_rotations(rotations_path)
        record = rotations.get(args.principal)
        if record is None or record['state'] not in ('issued', 'confirmed'):
            raise KeygenError(f'no rotation under way for {args.principal}')
        fields = dict(token.split('=', 1) for token in args.confirm_readback.split() if '=' in token)
        if fields.get('reachable') != 'yes':
            raise KeygenError('the read-back did not reach the service (reachable=yes is required)')
        if fields.get('key_expires') != record['new_expires_at']:
            raise KeygenError('the read-back does not show the new key (key_expires differs from the new key\'s expiry)')
        record['state'] = 'confirmed'
        record['confirmed_at'] = iso(utcnow())
        save_rotations(rotations_path, rotations)
    print(f'rotation of {args.principal} confirmed by the worker read-back; --retire-old may run')


def retire(args, policy: Path) -> None:
    rotations_path = Path(f'{policy}.rotation')
    with policy_lock(policy):
        document = load_policy(policy)
        rotations = load_rotations(rotations_path)
        record = rotations.get(args.principal)
        if record is None:
            raise KeygenError(f'no rotation under way for {args.principal}')
        if record['state'] != 'confirmed':
            raise KeygenError('refused: the enrichment worker\'s own read-back with the new key has not been confirmed')
        principal = find(document, args.principal)
        if principal is None or not any(entry['sha256'] == record['new_sha256'] for entry in principal['keys']):
            raise KeygenError('the new key is not in the policy; nothing is retired')
        principal['keys'] = [entry for entry in principal['keys'] if entry['sha256'] != record['old_sha256']]
        save_policy(policy, document)
        del rotations[args.principal]
        save_rotations(rotations_path, rotations)
    print(f'principal {args.principal}: old key removed; policy revision {document["revision"]}')


class Parser:
    """argparse without sys.exit: a usage error is a KeygenError."""

    def __init__(self) -> None:
        import argparse

        class Quiet(argparse.ArgumentParser):
            def error(self, message: str):  # type: ignore[override]  # raising instead of exiting
                raise KeygenError(message)

        parser = Quiet(prog='keygen', description=(__doc__ or '').split('\n')[0])
        parser.add_argument('--policy', default=DEFAULT_POLICY)
        parser.add_argument('--principal', required=True)
        parser.add_argument('--scopes')
        parser.add_argument('--max-jobs-per-day', type=int)
        parser.add_argument('--max-concurrent', type=int)
        parser.add_argument('--expires-days', type=int)
        parser.add_argument('--key-file')
        group = parser.add_mutually_exclusive_group()
        group.add_argument('--rotate', action='store_true')
        group.add_argument('--retire-old', action='store_true')
        group.add_argument('--confirm-readback')
        self.parser = parser

    def parse(self, argv: list[str]):
        args = self.parser.parse_args(argv)
        if not PRINCIPAL.match(args.principal):
            raise KeygenError('principal ids are lowercase letters, digits, dot, dash and underscore')
        if args.retire_old or args.confirm_readback is not None:
            return args
        if not args.key_file or not args.expires_days or args.expires_days < 1:
            raise KeygenError('--key-file and --expires-days (at least 1) are required')
        if not args.rotate:
            if not args.scopes or not args.max_jobs_per_day or not args.max_concurrent:
                raise KeygenError('--scopes, --max-jobs-per-day and --max-concurrent are required')
            if args.max_jobs_per_day < 1 or args.max_concurrent < 1:
                raise KeygenError('the caps must be at least 1')
        return args


def main(argv: list[str] | None = None) -> int:
    try:
        args = Parser().parse(sys.argv[1:] if argv is None else argv)
        policy = Path(args.policy)
        if args.rotate:
            rotate(args, policy)
        elif args.retire_old:
            retire(args, policy)
        elif args.confirm_readback is not None:
            confirm(args, policy)
        else:
            create(args, policy)
    except KeygenError as exc:
        print(f'keygen: {exc}', file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError) as exc:
        print(f'keygen: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
