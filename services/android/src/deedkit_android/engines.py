"""The two engines that can drive a local-agent run, Claude Code and Codex (ANYM-971 criteria 2 and 9).

Per engine: the argv, the environment (an allowlist, the engine's login only), the parser of its JSON-lines stream and
the status the host reports (configured, login, pinned version, passed probe). Nothing else depends on the engine."""

import asyncio
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time

PROVIDERS = {'claude': 'Anthropic', 'codex': 'OpenAI'}
PHONE_TOOLS = 'mcp__phone__'
# Codex 0.166.0 keeps a code-mode runner, sub-agent and input tools it cannot switch off; everything that reaches a shell,
# the web, a browser, another app or a file is off here. The build probe (engine_probe) decides what a version may do.
CODEX_OFF = ('shell_tool', 'unified_exec', 'apps', 'plugins', 'tool_suggest', 'multi_agent', 'browser_use',
             'browser_use_external', 'browser_use_full_cdp_access', 'computer_use', 'image_generation', 'view_image',
             'hooks', 'sleep_tool', 'goals', 'in_app_browser', 'shell_snapshot', 'unified_exec_tty',
             'write_stdin_approval')
DEFAULT_MODEL = {'claude': 'sonnet', 'codex': None}
KILL_GRACE_S = 5
TOKEN_KEYS = {'input': ('input_tokens',), 'output': ('output_tokens',),
              'cache_read': ('cache_read_input_tokens', 'cached_input_tokens'),
              'cache_write': ('cache_creation_input_tokens', 'cache_write_input_tokens')}
VERSION = re.compile(r'\d+\.\d+\.\d+(?:[-+.][0-9A-Za-z]+)*')
_versions = {}


def phone_command(run_file):
    """The stdio server the engine starts: this interpreter, isolated, the run's file."""
    return sys.executable, ['-I', '-m', 'deedkit_android.phone_mcp', '--run-file', str(run_file)]


def private_file(path, limit=4096):
    """The text of a 0600 regular file of the host, or None (missing, a link, readable by others, empty)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > limit:
            return None
        return os.read(fd, limit).decode(errors='replace').strip() or None
    finally:
        os.close(fd)


def login_env(name, config):
    """The one login variable of the engine, or None when the host holds no usable login."""
    if name == 'claude':
        token = private_file(config.token_file) if config.token_file else None
        return {'CLAUDE_CODE_OAUTH_TOKEN': token} if token else None
    home = Path(config.codex_home) if config.codex_home else None
    if home is None or not home.is_dir() or home.stat().st_mode & 0o077 or private_file(home / 'auth.json', 1 << 20) is None:
        return None
    return {'CODEX_HOME': str(home)}


def environment(name, config, run_dir):
    """PATH, HOME (the run's private directory) and the login: no other variable reaches the engine."""
    path = os.pathsep.join([str(Path(config.binary).parent), os.environ.get('PATH', '/usr/bin:/bin')])
    return {'PATH': path, 'HOME': str(run_dir), **(login_env(name, config) or {})}


def argv(name, config, run_dir, system_prompt):
    """The engine's command line. No shell, no skip-permissions or bypass flag, never `--bare` (Claude's `--bare` ignores
    CLAUDE_CODE_OAUTH_TOKEN, code.claude.com/docs/en/authentication). The prompt travels on standard input."""
    run_file = Path(run_dir) / 'run.json'
    command, arguments = phone_command(run_file)
    model = config.model or DEFAULT_MODEL[name]
    if name == 'claude':
        mcp = Path(run_dir) / 'mcp.json'
        mcp.write_text(json.dumps({'mcpServers': {'phone': {'command': command, 'args': arguments}}}))
        mcp.chmod(0o600)
        return [config.binary, '-p', '--output-format', 'stream-json', '--verbose', '--tools', '',
                '--strict-mcp-config', '--mcp-config', str(mcp), '--allowedTools', PHONE_TOOLS + '*',
                '--model', model, '--no-session-persistence', '--setting-sources', '', '--disable-slash-commands',
                '--system-prompt', system_prompt]
    flags = [item for feature in CODEX_OFF for item in ('--disable', feature)]
    return [config.binary, 'exec', '--json', '--ephemeral', '--ignore-user-config', '--ignore-rules',
            '--skip-git-repo-check', '-s', 'read-only', '-C', str(run_dir), *(['-m', model] if model else []),
            '-c', 'mcp_servers.phone.command=' + json.dumps(command),
            '-c', 'mcp_servers.phone.args=' + json.dumps(arguments),
            '-c', 'mcp_servers.phone.default_tools_approval_mode="approve"', '-c', 'web_search="disabled"',
            *flags, '-']


def stdin_text(name, system_prompt, task):
    return task if name == 'claude' else system_prompt + '\n\n' + task


async def spawn(name, config, run_dir, url, bearer, system_prompt, task):
    """Start the engine as its own process group in the run's private directory; its private marker names the group."""
    run_dir = Path(run_dir)
    run_file = run_dir / 'run.json'
    run_file.write_text(json.dumps({'url': url, 'bearer': bearer, 'timeout_s': 120}))
    run_file.chmod(0o600)
    command = argv(name, config, run_dir, system_prompt)
    process = await asyncio.create_subprocess_exec(
        *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env=environment(name, config, run_dir), cwd=run_dir, start_new_session=True, limit=4 * 1024 * 1024)
    marker = run_dir / 'marker.json'
    marker.write_text(json.dumps({'pid': process.pid, 'token': run_dir.name}))
    marker.chmod(0o600)
    try:
        pipe = process.stdin
        if pipe is not None:
            pipe.write(stdin_text(name, system_prompt, task).encode())
            await pipe.drain()
            pipe.close()
    except (OSError, ConnectionError):
        pass  # An engine that does not read its prompt ends on its own; its exit is the result.
    return process


def group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False  # macOS answers EPERM for a group that is only a zombie leader
    return True


async def kill_group(process, grace=None):
    """SIGTERM to the engine's process group, SIGKILL to what is left after `grace` seconds; no child stays behind."""
    grace = KILL_GRACE_S if grace is None else grace
    pgid = process.pid
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = asyncio.get_running_loop().time() + grace
    while group_alive(pgid) and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), 0.01)
            except TimeoutError:
                pass
    if group_alive(pgid):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await process.wait()


def kill_by_marker(marker):
    """After a service restart: stop the group a dead service left, only when its command line still carries the run's
    private token (a reused PID is never touched). `ps` because macOS has no /proc."""
    try:
        state = json.loads(Path(marker).read_text())
        pid, token = int(state['pid']), str(state['token'])
    except (OSError, ValueError, KeyError):
        return False
    if pid < 2 or not token.startswith('run-'):
        return False
    try:
        line = subprocess.run(['ps', '-ww', '-o', 'pgid=,command=', '-p', str(pid)], capture_output=True, text=True,
                              timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    if token not in line or not line.split(None, 1) or line.split(None, 1)[0] != str(pid):
        return False
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError):
            return True
        deadline = 50
        while group_alive(pid) and deadline:
            time.sleep(0.1)
            deadline -= 1
        if not group_alive(pid):
            return True
    return not group_alive(pid)


def installed_version(binary):
    """The binary's own version number, cached per file state; None when it does not run."""
    try:
        info = os.stat(binary)
    except OSError:
        return None
    key = (binary, info.st_mtime_ns, info.st_size)
    if key not in _versions:
        try:
            output = subprocess.run([binary, '--version'], capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL,
                                    env={'PATH': os.pathsep.join([str(Path(binary).parent), '/usr/bin:/bin']),
                                         'HOME': '/nonexistent'}).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        found = VERSION.search(output)
        _versions[key] = found[0] if found else None
    return _versions[key]


def probe_path(state_dir, name):
    return Path(state_dir) / ('engine-probe-' + name + '.json')


def read_probe(state_dir, name):
    try:
        return json.loads(probe_path(state_dir, name).read_text())
    except (OSError, ValueError):
        return None


def write_probe(state_dir, name, record):
    path = probe_path(state_dir, name)
    path.write_text(json.dumps(record, indent=1))
    path.chmod(0o600)


def status(state_dir, name, config, require_probe=True):
    """What `android_capabilities` shows for one engine: offered only when configured, logged in, at the pinned version and
    with a passed probe of that version. `reason` says why not (Codex: codex_probe_failed)."""
    shown = {'engine': name, 'provider': PROVIDERS[name], 'available': False}
    if config is None:
        return {**shown, 'reason': 'not_configured'}
    shown.update(version=config.version, model=config.model or DEFAULT_MODEL[name])
    if login_env(name, config) is None:
        return {**shown, 'reason': 'login_missing'}
    if installed_version(config.binary) != config.version:
        return {**shown, 'reason': 'version_mismatch'}
    if not require_probe:
        return {**shown, 'available': True}
    record = read_probe(state_dir, name)
    if not record or record.get('passed') is not True or record.get('version') != config.version:
        return {**shown, 'reason': 'codex_probe_failed' if name == 'codex' else 'probe_failed'}
    return {**shown, 'available': True}


def short(text, limit=300):
    return ' '.join(str(text).split())[:limit]


class Stream:
    """One engine's JSON-lines output, read in memory and never stored: notes, the tools it used, its token counts."""

    def __init__(self, name):
        self.name, self.model, self.tools = name, None, []
        self.calls, self.foreign, self.errors = [], [], []  # tool calls made; non-phone work; engine errors
        self.messages, self.final = {}, {}

    def note(self, text):
        return short(text) or None

    def feed(self, line):
        """Parse one line; return the short text the engine said in it, if any."""
        try:
            event = json.loads(line)
        except ValueError:
            return None
        if not isinstance(event, dict):
            return None
        try:
            return (self.claude if self.name == 'claude' else self.codex)(event)
        except (TypeError, AttributeError, ValueError):
            return None  # A line of an unexpected shape is skipped, never fatal.

    def claude(self, event):
        kind = event.get('type')
        if kind == 'system' and event.get('subtype') == 'init':
            self.tools, self.model = list(event.get('tools') or []), event.get('model')
        elif kind == 'assistant':
            message = event.get('message') or {}
            if message.get('usage'):
                self.messages[message.get('id') or len(self.messages)] = message['usage']
            said = None
            for block in message.get('content') or []:
                if block.get('type') == 'text':
                    said = self.note(block.get('text', ''))
                elif block.get('type') == 'tool_use':
                    self.calls.append(block.get('name', ''))
                    if not str(block.get('name', '')).startswith(PHONE_TOOLS):
                        self.foreign.append(block.get('name', ''))
            return said
        elif kind == 'result':
            self.final = event.get('usage') or {}
            if event.get('is_error'):
                self.errors.append(short(event.get('result', 'error')))
        return None

    def codex(self, event):
        kind, item = event.get('type'), event.get('item') or {}
        if kind in {'error', 'turn.failed'}:
            body = event.get('error') if isinstance(event.get('error'), dict) else event
            self.errors.append(short(body.get('message', 'error')))
        elif kind == 'turn.completed':
            self.messages[len(self.messages)] = event.get('usage') or {}
        elif kind in {'item.started', 'item.completed'}:
            item_type = item.get('type')
            if item_type == 'mcp_tool_call' and kind == 'item.started':
                call = f"{item.get('server')}/{item.get('tool')}"
                self.calls.append(call)
                if item.get('server') != 'phone':
                    self.foreign.append(call)
            elif item_type == 'mcp_tool_call' and item.get('error'):
                self.errors.append(short((item['error'] or {}).get('message', 'tool error')))
            elif item_type not in {'agent_message', 'mcp_tool_call', 'reasoning', 'error', None} and kind == 'item.completed':
                self.foreign.append(item_type)
            elif item_type == 'agent_message' and kind == 'item.completed':
                return self.note(item.get('text', ''))
        return None

    def tokens(self):
        """Normalized counts: the engine's final report, else the sum of its per-message reports."""
        usages = [self.final] if self.final else list(self.messages.values())
        count = [usage for usage in usages if isinstance(usage, dict)]
        return {name: sum(usage[key] for usage in count for key in keys
                          if type(usage.get(key)) is int and usage[key] > 0) for name, keys in TOKEN_KEYS.items()}


def verdict(name, stream, backend_calls):
    """The probe's failed checks (none: passed). The engine saw the phone server's tools only, no non-phone tool ran when
    the goal asked for a shell, a file and the web, and its phone calls ran without an approval prompt."""
    failed = []
    if name == 'claude' and (not stream.tools or any(not tool.startswith(PHONE_TOOLS) for tool in stream.tools)):
        failed.append('tool_list_not_phone_only')
    if stream.foreign:
        failed.append('non_phone_tool_used')
    names = [call.rsplit('/', 1)[-1] for call in backend_calls]
    if 'screen' not in names or 'finish' not in names:
        failed.append('phone_call_not_run' if not backend_calls else 'phone_calls_incomplete')
    if any('approval' in error.lower() for error in stream.errors):
        failed.append('approval_prompt')
    return failed
