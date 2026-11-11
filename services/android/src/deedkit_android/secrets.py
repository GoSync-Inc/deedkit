"""App secrets held on the phone host (ANYM-969).

The owner stores a value with `deedkit-android secret set NAME --package PKG` (standard input, never an argument). A flow
names it as `${secret:NAME}`: the worker resolves it only when it types, into a focused editable field of the app the
secret is bound to. The value is never part of a request, a compiled flow, a receipt or a log, and every read of a run
that typed it shows the reference text instead (Masker). No HTTP route or tool sets, reads or lists a value.
"""

import argparse
import fcntl
import getpass
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid

from .config import PACKAGE, Configuration, Refused

NAME = re.compile(r'[A-Z][A-Z0-9_]{0,63}')
# Payment PINs and card data are the owner's hands; the real control is the guard's PIN-screen stop (ANYM-968).
FORBIDDEN = ('PIN', 'CVV', 'CVC', 'CARD', 'OTP', 'PASSCODE')
REFERENCE = re.compile(r'\$\{secret:([A-Z][A-Z0-9_]{0,63})\}')
MAX_VALUE = 256
MAX_FILE = 1 << 20
FILE = 'secrets.json'
# Accessibility classes of editable fields (EditText subclasses report EditText).
TEXT_FIELDS = ('EditText', 'AutoCompleteTextView')


def check_name(name):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise Refused('secret_name_invalid', 422)
    if any(word in name for word in FORBIDDEN):
        raise Refused('secret_name_forbidden', 422)


def check_package(package):
    if not isinstance(package, str) or len(package) > 256 or not re.fullmatch(PACKAGE, package):
        raise Refused('secret_package_invalid', 422)


def check_value(value):
    if not isinstance(value, str) or not value or any(char in value for char in '\r\n\x00'):
        raise Refused('secret_value_invalid', 422)
    if len(value) > MAX_VALUE:
        raise Refused('secret_value_too_long', 422)


class SecretStore:
    """`<state_dir>/secrets.json`: 0600, replaced atomically, read again on every use so a change needs no restart."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / FILE

    def read(self):
        """{name: {package, value}}. A missing file is empty; one open to others or malformed is refused."""
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {}
        except OSError:
            raise Refused('secrets_unavailable', 503) from None
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > MAX_FILE:
                raise Refused('secrets_unavailable', 503)
            data = stream.read(MAX_FILE + 1)
        try:
            found = {}
            for name, entry in json.loads(data)['secrets'].items():
                check_name(name)
                check_package(entry['package'])
                check_value(entry['value'])
                found[name] = {'package': entry['package'], 'value': entry['value']}
            return found
        except (ValueError, KeyError, TypeError, AttributeError, Refused):
            raise Refused('secrets_unavailable', 503) from None

    def packages(self):
        return {name: entry['package'] for name, entry in self.read().items()}

    def listing(self):
        """Names and packages only."""
        return [{'name': name, 'package': package} for name, package in sorted(self.packages().items())]

    def update(self, change):
        # One writer at a time (a separate lock file: the service holds service.lock for its whole life).
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        lock = os.open(self.directory / 'secrets.lock', os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            found = self.read()
            change(found)
            self.write(found)
        finally:
            os.close(lock)

    def write(self, found):
        temporary = self.directory / f'{FILE}.{uuid.uuid4().hex}.tmp'
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump({'version': 1, 'secrets': found}, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        directory = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def set(self, name, package, value):
        check_name(name)
        check_package(package)
        check_value(value)
        self.update(lambda found: found.update({name: {'package': package, 'value': value}}))

    def delete(self, name):
        def remove(found):
            if name not in found:
                raise Refused('secret_unknown', 404)
            del found[name]
        self.update(remove)


def mentions(item):
    """True when a loaded document holds secret reference syntax in any string or key."""
    if isinstance(item, str):
        return '${secret:' in item
    if isinstance(item, list):
        return any(mentions(child) for child in item)
    if isinstance(item, dict):
        return any(mentions(key) or mentions(child) for key, child in item.items())
    return False


def references(item):
    """Names of the whole-value references in compiled commands. A map's internals are never searched."""
    found = set()

    def walk(value):
        if isinstance(value, str):
            match = REFERENCE.fullmatch(value)
            if match:
                found.add(match[1])
        elif isinstance(value, list):
            for child in value:
                walk(child)
        elif isinstance(value, dict):
            for key, child in value.items():
                if key not in {'app_map', 'map_ref'}:
                    walk(child)
    walk(item)
    return found


def current_package(device):
    try:
        return device.app_current().get('package')
    except Exception:  # noqa: BLE001 - an unreadable foreground is a mismatch below
        return None


def typed_text(device, text, secrets, context, field):
    """The text to type: `text`, or for a whole-value reference the secret's value.

    That needs the app on screen (the guard's reading of this action, else a fresh one) and `field` (XML attributes of
    the target) to be the secret's package, and the field to be a focused editable one. Otherwise nothing is typed."""
    found = REFERENCE.fullmatch(text)
    if found is None:
        return text
    name = found[1]
    entry = (secrets or {}).get(name)
    if entry is None:
        error = Refused('secret_unknown', 422)
        error.details = {'secret': name}
        raise error
    bound = entry['package']
    foreground = context['package'] if context else current_package(device)
    reason = None
    if foreground != bound:
        reason = 'foreground_package'
    elif field is None or not (field.get('class') or '').endswith(TEXT_FIELDS) or field.get('focused') != 'true':
        reason = 'field_not_editable_focused'
    elif field.get('package') != bound:
        reason = 'field_package'
    if reason:
        error = Refused('secret_package_mismatch')
        error.details = {'secret': name, 'reason': reason, 'bound_package': bound, 'foreground_package': foreground}
        raise error
    return entry['value']


class Masker:
    """The values typed in one run, in memory only: each occurrence in a later read becomes its reference text."""

    def __init__(self):
        self.values, self.names, self.pattern = {}, {}, REFERENCE

    def __bool__(self):
        return bool(self.values)

    def add(self, name, value):
        self.values[name] = value
        self.names = {value: name for name, value in self.values.items()}
        # A reference already in the text matches first and stays whole, even when the value is part of its spelling.
        longest_first = sorted(self.names, key=len, reverse=True)
        self.pattern = re.compile('|'.join([REFERENCE.pattern, *map(re.escape, longest_first)]))

    def replace(self, match):
        return '${secret:%s}' % self.names[match[0]] if match[0] in self.names else match[0]

    def mask(self, item):
        if not self.values:
            return item
        if isinstance(item, str):
            return self.pattern.sub(self.replace, item)
        if isinstance(item, list):
            return [self.mask(child) for child in item]
        if isinstance(item, tuple):
            return tuple(self.mask(child) for child in item)
        if isinstance(item, dict):
            masked = {key: self.mask(child) for key, child in item.items()}
            if isinstance(masked.get('text'), str) and isinstance(masked.get('words'), list) \
                    and masked['text'] != item['text']:
                masked['words'] = []  # OCR words of a masked line would spell the value out.
            return masked
        return item


class Run:
    """The secrets of one flow run: what each worker call may type and what to mask afterwards. Memory only."""

    def __init__(self, store):
        self.store, self.bound, self.packages, self.masker = store, {}, [], Masker()

    def bind(self, commands):
        names = references(commands)
        if not names:
            return
        known = self.store.read()
        if names - known.keys():
            raise Refused('secret_unknown', 422)
        self.bound = {name: known[name] for name in names}
        self.packages = sorted({entry['package'] for entry in self.bound.values()})

    def attach(self, command):
        """Call options for one command: the apps whose password fields the run expects, and the values it types."""
        if not self.bound:
            return {}
        options: dict[str, object] = {'secret_packages': self.packages}
        names = references(command)
        if names:
            options['secrets'] = {name: self.bound[name] for name in names}
            for name in names:
                self.masker.add(name, self.bound[name]['value'])  # Before the send: a lost response is masked too.
        return options

    def mask(self, item):
        return self.masker.mask(item)

    async def call(self, send, command, **options):
        try:
            return self.masker.mask(await send(command, **options, **self.attach(command)))
        except Refused as error:
            for key in ('details', 'hint', 'diagnostic', 'guard'):
                if hasattr(error, key):
                    setattr(error, key, self.masker.mask(getattr(error, key)))
            raise


def read_value(stdin, name):
    if stdin.isatty():
        value = getpass.getpass(f'Value for {name} (not shown): ')
    else:
        value = stdin.read(MAX_VALUE + 3)
        value = value[:-2] if value.endswith('\r\n') else value[:-1] if value.endswith('\n') else value
    check_value(value)
    return value


def fail(code):
    print(code, file=sys.stderr)
    return 2


def main(argv=None, stdin=None):
    """`deedkit-android secret set|list|delete`. Values come from standard input and are never printed."""
    parser = argparse.ArgumentParser(prog='deedkit-android secret',
                                     description='App secrets stored on this host, typed into one app by ${secret:NAME}.')
    actions = parser.add_subparsers(dest='action', required=True)
    for action in ('set', 'list', 'delete'):
        sub = actions.add_parser(action)
        if action != 'list':
            sub.add_argument('name')
        if action == 'set':
            sub.add_argument('--package', required=True, help='the only app the value may be typed into')
        sub.add_argument('--config', required=True)
    args = parser.parse_args(argv)
    try:
        store = SecretStore(Configuration(args.config).initial.state_dir)
    except (OSError, ValueError):
        return fail('configuration_unavailable')
    try:
        if args.action == 'set':
            check_name(args.name)
            check_package(args.package)
            store.set(args.name, args.package, read_value(sys.stdin if stdin is None else stdin, args.name))
        elif args.action == 'list':
            for name, package in sorted(store.packages().items()):
                print(f'{name}\t{package}')
        else:
            store.delete(args.name)
    except Refused as error:
        print(error.code, file=sys.stderr)
        return 1 if error.status == 404 else 2
    except OSError:
        return fail('secrets_unavailable')
    return 0
