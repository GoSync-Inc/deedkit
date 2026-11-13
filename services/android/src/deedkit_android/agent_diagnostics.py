"""Bounded native error receipts. No raw stderr, request bodies or phone dumps are persisted."""

import re
import time


class NativeContext:
    """Observed per-turn native metadata; context size is not an auto-compaction threshold."""

    TOKEN_FIELDS = ('inputTokens', 'cachedInputTokens', 'cacheWriteInputTokens',
                    'outputTokens', 'reasoningOutputTokens', 'totalTokens')

    def __init__(self):
        self.thread_id = self.turn_id = self.usage = self.window = None
        self.compactions = {}

    def bind(self, thread_id, turn_id):
        if self.turn_id is None and all(isinstance(v, str) and v for v in (thread_id, turn_id)):
            self.thread_id, self.turn_id = thread_id, turn_id

    def observe(self, event):
        params = event.get('params') or {}
        if (self.turn_id is None or params.get('threadId') != self.thread_id
                or params.get('turnId') != self.turn_id):
            return
        method = event.get('method')
        if method == 'thread/tokenUsage/updated':
            usage = params.get('tokenUsage')
            if not isinstance(usage, dict):
                return
            self.usage = {}
            for section in ('last', 'total'):
                values = usage.get(section)
                self.usage[section] = {key: values[key] for key in self.TOKEN_FIELDS
                    if isinstance(values, dict) and type(values.get(key)) is int and values[key] >= 0}
            value = usage.get('modelContextWindow')
            self.window = value if type(value) is int and value > 0 else None
        elif method in ('item/started', 'item/completed'):
            item = params.get('item') or {}
            ident = item.get('id')
            if item.get('type') == 'contextCompaction' and isinstance(ident, str) and ident:
                row = self.compactions.setdefault(ident, {'item_id': ident, 'started': False, 'completed': False})
                row['started' if method == 'item/started' else 'completed'] = True

    def receipt(self):
        return {'native_thread_id': self.thread_id, 'native_turn_id': self.turn_id,
            'token_usage': {key: dict(value) for key, value in self.usage.items()} if self.usage is not None else None,
            'model_context_window': self.window, 'auto_compact_limit_tokens': None,
            'compactions': [dict(value) for value in self.compactions.values()]}


class NativeTiming:
    """Host arrival intervals, never provider/model time or proof of command quiescence."""

    STOP_SOURCES = frozenset(('deadline', 'owner_stop', 'service_shutdown', 'action_cap',
        'adapter_failure', 'adapter_cancelled', 'native_disconnected', 'model_rerouted', 'supervision_revoked'))

    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.started = self.phase_started = clock()
        self.ended, self.initiator, self.terminal = None, None, None
        self.thread_id, self.turn_id = None, None
        self.current_phase, self.phases = 'connect', {}
        self.commands, self.questions = set(), set()
        self.seen_commands = set()
        self.command_started = self.question_started = None
        self.command_seconds = self.question_seconds = 0
        self.commands_started = self.commands_completed = self.blocking_questions = 0

    def phase(self, name):
        if self.ended is not None or name not in ('preflight', 'submission', 'native_turn') or name == self.current_phase:
            return
        now = self.clock()
        self.phases[self.current_phase] = self.phases.get(self.current_phase, 0) + now - self.phase_started
        self.current_phase, self.phase_started = name, now

    def bind(self, thread_id, turn_id):
        if self.turn_id is None and self.ended is None:
            self.thread_id, self.turn_id = thread_id, turn_id
            self.phase('native_turn')

    def request_stop(self, source):
        if self.initiator is None and source in self.STOP_SOURCES:
            self.initiator = source

    def finish(self, source):
        if self.ended is not None:
            return
        self.ended = self.clock()
        self.phases[self.current_phase] = self.phases.get(self.current_phase, 0) + self.ended - self.phase_started
        if self.command_started is not None:
            self.command_seconds += self.ended - self.command_started
            self.command_started = None
        if self.question_started is not None:
            self.question_seconds += self.ended - self.question_started
            self.question_started = None
        self.source = source if source in self.STOP_SOURCES or source in (
            'native_completed', 'native_failed', 'native_interrupted') else 'native_terminal_unknown'

    def observe(self, event):
        params = event.get('params') or {}
        method, item = event.get('method'), params.get('item') or {}
        turn = params.get('turnId') or (params.get('turn') or {}).get('id')
        if (self.ended is not None or self.turn_id is None or params.get('threadId') != self.thread_id
                or (turn != self.turn_id and not (method == 'serverRequest/resolved' and turn is None))):
            return
        now = self.clock()
        if method in ('item/started', 'item/completed') and item.get('type') == 'commandExecution':
            ident = item.get('id')
            if not isinstance(ident, str):
                return
            if method == 'item/started' and ident not in self.seen_commands:
                if not self.commands:
                    self.command_started = now
                self.commands.add(ident)
                self.seen_commands.add(ident)
                self.commands_started += 1
            elif method == 'item/completed' and ident in self.commands:
                self.commands.remove(ident)
                self.commands_completed += 1
                if not self.commands:
                    self.command_seconds += now - self.command_started
                    self.command_started = None
        elif method == 'item/tool/requestUserInput' and params.get('isBlocking') is True:
            ident = event.get('id')
            if type(ident) not in (str, int) or (type(ident), ident) in self.questions:
                return
            if not self.questions:
                self.question_started = now
            self.questions.add((type(ident), ident))
            self.blocking_questions += 1
        elif method == 'serverRequest/resolved':
            ident = params.get('requestId')
            if type(ident) in (str, int) and (type(ident), ident) in self.questions:
                self.questions.remove((type(ident), ident))
                if not self.questions:
                    self.question_seconds += now - self.question_started
                    self.question_started = None
        elif method == 'turn/completed':
            status = (params.get('turn') or {}).get('status')
            self.terminal = status if status in ('completed', 'interrupted', 'failed') else 'unknown'
            self.finish('native_' + self.terminal)

    def receipt(self):
        now = self.ended if self.ended is not None else self.clock()
        phases = dict(self.phases)
        if self.ended is None:
            phases[self.current_phase] = phases.get(self.current_phase, 0) + now - self.phase_started
        return {'native_thread_id': self.thread_id, 'native_turn_id': self.turn_id,
            'phase_ms': {key: round(value * 1000, 2) for key, value in phases.items()},
            'elapsed_ms': round((now - self.started) * 1000, 2),
            'command_wall_ms': round((self.command_seconds + (now - self.command_started if self.command_started is not None else 0)) * 1000, 2),
            'blocking_question_pending_ms': round((self.question_seconds + (now - self.question_started if self.question_started is not None else 0)) * 1000, 2),
            'commands_started': self.commands_started, 'commands_completed': self.commands_completed,
            'commands_incomplete': len(self.commands), 'blocking_questions': self.blocking_questions,
            'stop_source': self.initiator or getattr(self, 'source', 'running'), 'native_terminal': self.terminal,
            'model_time_observed': False, 'network_time_observed': False}


def message(text, private=()):
    text = str(text)
    if any(item in text.lower() for item in ('<hierarchy', '<node ', 'begin private', '"messages":', '"input":[')):
        return '<non-diagnostic content withheld>'
    for value in private:
        if len(str(value)) >= 3:
            text = text.replace(str(value), '<task-value>')
    text = re.sub(r'\x1b\[[0-9;]*m', '', text)
    text = re.sub(r'''(?i)(\b(?:authorization|proxy-authorization|cookie|set-cookie)["']?\s*[:=]\s*)[^\r\n]+''',
                  r'\1<redacted>', text)
    text = re.sub(r'''(?i)((?:authorization|bearer|api[_-]?key|(?:access[_-]?|refresh[_-]?)?token|password|secret)["']?\s*[:=]?\s*["']?)[^\s,;]+''',
                  r'\1<redacted>', text)
    text = re.sub(r'(?:sk-|eyJ)[A-Za-z0-9_.-]{10,}|[A-Za-z0-9_-]{48,}', '<redacted>', text)
    text = re.sub(r'https?://[^\s)]+', '<url-redacted>', text)
    text = re.sub(r'[^\s@]+@[^\s@]+', '<email-redacted>', text)
    return ' '.join(text.split())[:700]


def receipt(stream_errors, stderr, private=()):
    found = []
    for source, lines in (('stream', stream_errors), ('stderr', stderr.decode(errors='replace').splitlines())):
        for line in lines:
            if source == 'stderr' and not re.search(r'(?i)\b(error|warn(?:ing)?|failed|denied|timeout|disconnected)\b', line):
                continue
            item = {'source': source, 'message': message(line, private)}
            if item not in found:
                found.append(item)
    return found[-8:]
