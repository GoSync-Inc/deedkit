"""One configured existing BB conversation; no queue, model or phone driver of our own."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import uuid

from pydantic import Field, ValidationError, model_validator
from typing import Literal

from .config import Refused, Strict

LIMIT = 131072


class TaskCall(Strict):
    device_id: str
    action: Literal['message', 'status', 'result', 'receipt']
    request_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    text: str | None = Field(default=None, min_length=1, max_length=32768, pattern=r"\S")

    @model_validator(mode='before')
    @classmethod
    def action_fields(cls, value):
        if not isinstance(value, dict):
            return value
        fields = {'message': {'request_id', 'text'}, 'receipt': {'request_id'}, 'status': set(), 'result': set()}
        required = fields.get(value.get('action'))
        if required is not None:
            errors = [{'type': 'missing', 'loc': (key,), 'input': value}
                      for key in sorted(required) if value.get(key) is None]
            errors += [{'type': 'extra_forbidden', 'loc': (key,), 'input': value[key]}
                       for key in ('request_id', 'text') if key not in required and value.get(key) is not None]
            if errors:
                raise ValidationError.from_exception_data(cls.__name__, errors)
        return value


def private_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise Refused('private_receipt_required')
        if info.st_size > LIMIT:
            raise Refused('receipt_too_large')
        value = json.load(stream)
        if not isinstance(value, dict):
            raise Refused('receipt_invalid')
        return value


def run_cli(target, args, text=None):
    if hashlib.sha256(Path(target.cli).read_bytes()).hexdigest() != target.cli_sha256:
        raise Refused('bb_cli_changed')
    env = dict(os.environ, BB_CLI=target.cli, BB_SERVER_URL=target.server_url)
    env.pop('BB_THREAD_ID', None)  # Owner message, not a send attributed to another agent.
    process = subprocess.Popen([target.node, target.cli, *args], env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)
    try:
        stdout, _ = process.communicate(text, timeout=30)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
        except ProcessLookupError:
            process.wait()
        raise
    if process.returncode != 0 or len(stdout) > 4 * 1024 * 1024:
        raise Refused('bb_command_unconfirmed')
    value = json.loads(stdout)
    if not isinstance(value, dict) or value.get('ok') is False:
        raise Refused('bb_command_unconfirmed')
    return value


class Conversation:
    def __init__(self, target, journal, authorize=lambda: None, run=None):
        self.target, self.authorize = target, authorize
        self.journal = Path(journal).absolute()
        if self.journal.resolve() != self.journal:
            raise Refused('private_journal_required')
        if self.journal.exists():
            self.check_directory(self.journal)
        self.run = run or run_cli

    @staticmethod
    def check_directory(path):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise Refused('private_journal_required')

    def _run(self, args, text=None):
        self.authorize()
        try:
            return self.run(self.target, args, text)
        except (OSError, ValueError, TimeoutError, subprocess.TimeoutExpired):
            raise Refused('bb_transport_unconfirmed', 503) from None

    def _metadata(self):
        value = self._run(['thread', 'show', self.target.thread_id, '--json'])
        thread, environment = value.get('thread', {}), value.get('environment') or {}
        if (thread.get('id') != self.target.thread_id or thread.get('projectId') != self.target.project_id
                or thread.get('environmentId') != self.target.environment_id or thread.get('providerId') != self.target.provider
                or environment.get('id') != self.target.environment_id or environment.get('hostId') != self.target.host_id
                or environment.get('path') != self.target.workspace):
            raise Refused('conversation_binding_changed')
        return thread

    def status(self):
        thread = self._metadata()
        return {'task_id': self.target.thread_id, 'scope': 'conversation', 'status': thread.get('status'),
                'queued_messages': thread.get('queuedMessageCount')}

    def result(self):
        status = self.status()
        if status['status'] != 'idle' or status['queued_messages'] != 0:
            return {**status, 'result': None}
        value = self._run(['thread', 'output', self.target.thread_id, '--json'])
        return {**status, 'scope': 'latest_conversation_result', 'result': value.get('output')}

    def _path(self, request_id):
        try:
            if str(uuid.UUID(request_id)) != request_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise Refused('invalid_request_id') from None
        return self.journal / ('bb-message-' + request_id + '.json')

    def receipt(self, request_id):
        self.authorize()
        try:
            value = private_json(self._path(request_id))
        except (OSError, ValueError):
            raise Refused('receipt_unavailable_do_not_resend') from None
        if value.get('task_id') != self.target.thread_id or value.get('request_id') != request_id:
            raise Refused('receipt_binding_changed')
        return {key: val for key, val in value.items() if key != 'digest'}

    def message(self, request_id, text):
        path = self._path(request_id)
        if not isinstance(text, str) or not text.strip() or len(text) > 32768:
            raise Refused('invalid_message')
        digest = hashlib.sha256(text.encode()).hexdigest()
        if path.exists() or path.is_symlink():
            try:
                old = private_json(path)
            except (OSError, ValueError):
                raise Refused('receipt_unavailable_do_not_resend') from None
            if old.get('digest') != digest:
                raise Refused('request_id_conflict')
            return self.receipt(request_id)
        thread = self._metadata()
        if thread.get('archivedAt') or thread.get('deletedAt') or thread.get('status') == 'stopping':
            raise Refused('conversation_not_writable')
        for directory in (self.journal.parent, self.journal):
            directory.mkdir(mode=0o700, exist_ok=True)
            self.check_directory(directory)
        record = {'task_id': self.target.thread_id, 'request_id': request_id, 'digest': digest, 'delivery': 'unknown'}
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            # Another process owns this send; never issue a second CLI message.
            raise Refused('receipt_pending_read_original') from None
        with os.fdopen(fd, 'w') as stream:
            json.dump(record, stream)
            stream.flush()
            os.fsync(stream.fileno())
        self._sync_directory()
        try:
            value = self._run(['thread', 'tell', self.target.thread_id, '--mode', 'auto', '--message-file', '-', '--json'], text)
            if value.get('ok') is True and value.get('delivery') in {'sent', 'queued'}:
                record['delivery'] = value['delivery']
                queued = value.get('queuedMessage')
                if isinstance(queued, dict) and isinstance(queued.get('id'), str):
                    record['queued_message_id'] = queued['id']
        except (OSError, ValueError, TimeoutError, Refused, subprocess.TimeoutExpired):
            pass  # Original intent stays unknown; no retry and no replacement request ID.
        temporary = path.with_suffix('.accepted')
        with temporary.open('x') as stream:
            os.chmod(temporary, 0o600)
            json.dump(record, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        self._sync_directory()
        return self.receipt(request_id)

    def _sync_directory(self):
        fd = os.open(self.journal, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def call(self, arguments):
        if not isinstance(arguments, dict):
            raise Refused('invalid_arguments')
        action = arguments.get('action')
        fields = {'message': {'action', 'request_id', 'text'}, 'status': {'action'},
                  'result': {'action'}, 'receipt': {'action', 'request_id'}}
        if action not in fields or set(arguments) != fields[action]:
            raise Refused('invalid_arguments')
        if action == 'message':
            return self.message(arguments['request_id'], arguments['text'])
        if action == 'receipt':
            return self.receipt(arguments['request_id'])
        return self.status() if action == 'status' else self.result()


def _target(configuration):
    try:
        return configuration.read().bb_conversation
    except (OSError, ValueError):
        raise Refused('configuration_unavailable', 503) from None


def configured_for(configuration, owner):
    target = _target(configuration)
    return target is not None and target.principal_id == owner


def binding(configuration, authorization, device=None):
    principal = configuration.authenticate(authorization)
    if 'android:control' not in principal.permissions:
        raise Refused('forbidden', 403)
    target = _target(configuration)
    if target is None:
        raise Refused('bb_conversation_disabled', 503)
    if (target.principal_id != principal.id or target.device_id not in principal.devices
            or device is not None and device != target.device_id):
        raise Refused('not_found', 404)
    return principal, target


def capabilities(configuration, authorization, schemas):
    principal = configuration.authenticate(authorization)
    target = _target(configuration)
    enabled = bool(target and target.principal_id == principal.id and target.device_id in principal.devices
                   and 'android:control' in principal.permissions)
    return {'service': 'deedkit-android', 'permissions': principal.permissions,
            'devices': [{'device_id': name} for name in principal.devices],
            'device_status_observed': False,
            'tool_manifest': {'version': 1, 'names': list(schemas)},
            'input_schemas': {name: schema.model_json_schema() for name, schema in schemas.items()},
            'bb_conversation': {'enabled': enabled, 'scope': 'conversation', 'tool': 'android_task'}}


async def handle(configuration, authorization, store, values):
    principal, target = binding(configuration, authorization, values['device_id'])

    def authorize():
        current, selected = binding(configuration, authorization, values['device_id'])
        if current.id != principal.id or selected != target:
            raise Refused('conversation_binding_changed')

    namespace = hashlib.sha256((principal.id + '\0' + target.device_id).encode()).hexdigest()
    conversation = Conversation(target, store.directory / 'bb-conversation' / namespace, authorize)
    arguments = {key: value for key, value in values.items() if key != 'device_id' and value is not None}
    return await asyncio.to_thread(conversation.call, arguments)
