"""Private persistent Maestro MCP transport with one bounded request at a time."""

import asyncio
import json
import os
import tempfile
import sys
import shutil
from pathlib import Path

from .config import Refused


class Maestro:
    def __init__(self, config, lock_fd=None):
        self.config = config
        self.lock_fd = lock_fd
        self.process = None
        self.lock = asyncio.Lock()
        self.sequence = 0
        self.temporary = None

    async def close(self):
        # Import here to keep the process cleanup implementation shared with ADB.
        from .device import stop_process
        process, self.process = self.process, None
        if process is not None:
            await stop_process(process)
        if self.temporary is not None:
            self.temporary.cleanup()
            self.temporary = None

    async def exchange(self, method, params=None, notification=False):
        self.sequence += 1
        message = {'jsonrpc': '2.0', 'method': method}
        if not notification:
            message['id'] = self.sequence
        if params is not None:
            message['params'] = params
        self.process.stdin.write(json.dumps(message).encode() + b'\n')
        await self.process.stdin.drain()
        if notification:
            return None
        while True:
            line = await self.process.stdout.readline()
            if not line:
                raise Refused('maestro_disconnected', 503)
            try:
                reply = json.loads(line)
            except ValueError:
                continue  # Some CLI versions print a startup banner.
            if not isinstance(reply, dict) or reply.get('id') != message['id']:
                continue
            if 'error' in reply:
                raise Refused('maestro_protocol_error', 502)
            return reply.get('result', {})

    async def start(self):
        if self.process is not None and self.process.returncode is None:
            return
        if not self.config.maestro:
            raise Refused('maestro_not_configured', 503)
        await self.close()
        directory = Path(self.config.state_dir) / 'maestro'
        directory.mkdir(mode=0o700, exist_ok=True)
        # The guardian inherits the exclusive state lock until its JVM has stopped.
        # Therefore a fresh service holding that lock can remove prior crash leftovers.
        if self.lock_fd is not None:
            for previous in directory.glob('run-*'):
                if previous.is_dir() and not previous.is_symlink():
                    shutil.rmtree(previous)
        self.temporary = tempfile.TemporaryDirectory(prefix="run-", dir=directory)
        # Never pass backend tokens or the service's other environment into flows.
        env = {key: os.environ[key] for key in ('HOME', 'TMPDIR', 'LANG') if key in os.environ}
        env.update({'PATH': str(Path(self.config.adb).parent) + ':/usr/bin:/bin:/usr/sbin:/sbin',
                    'MAESTRO_CLI_NO_ANALYTICS': '1', 'MAESTRO_CLI_NO_UPDATE_CHECK': '1',
                    'JAVA_TOOL_OPTIONS': '-Djava.io.tmpdir=' + self.temporary.name})
        if self.config.java_home:
            env['JAVA_HOME'] = self.config.java_home
        self.process = await asyncio.create_subprocess_exec(
            sys.executable, str(Path(__file__).with_name('guardian.py')), str(os.getpid()), self.temporary.name,
            self.config.maestro, 'mcp', '--no-viewer', '--working-dir', str(directory),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=env, start_new_session=True,
            limit=4 * 1024 * 1024, pass_fds=() if self.lock_fd is None else (self.lock_fd,),
        )
        await self.exchange('initialize', {'protocolVersion': '2024-11-05',
                            'capabilities': {}, 'clientInfo': {'name': 'deedkit-android', 'version': '0.1'}})
        await self.exchange('notifications/initialized', notification=True)

    async def call(self, name, arguments, timeout):
        # Waiting behind another device is part of the caller's total deadline.
        async with asyncio.timeout(timeout):
            async with self.lock:
                try:
                    await self.start()
                    result = await self.exchange('tools/call', {'name': name, 'arguments': arguments})
                    if result.get('isError'):
                        error = Refused('maestro_action_failed')
                        error.diagnostic = ' '.join(item.get('text', '') for item in result.get('content', [])
                                                    if item.get('type') == 'text')[:2000]
                        raise error
                    texts = [item['text'] for item in result.get('content', []) if item.get('type') == 'text']
                    if not texts:
                        raise Refused('maestro_empty_result', 502)
                    return json.loads(texts[0])
                except BaseException:
                    # Only the call holding this lock can stop the shared process.
                    await self.close()
                    raise


def screen_elements(hierarchy):
    """Preserve the ADB attribute names and omit editable text absent password flags."""
    names = {'b': 'bounds', 'txt': 'text', 'rid': 'resource-id', 'a11y': 'content-desc',
             'hint': 'hint', 'cls': 'class', 'scroll': 'scrollable'}
    elements = []

    def visit(nodes):
        for node in nodes:
            item = {names.get(key, key): str(value).lower() if isinstance(value, bool) else value
                    for key, value in node.items() if key != 'c'}
            # Maestro's compact schema omits isPassword, so conservatively mask all EditTexts.
            if str(item.get('class', '')).endswith('EditText'):
                for key in ('text', 'content-desc', 'val'):
                    item.pop(key, None)
            elements.append(item)
            visit(node.get('c', []))
    visit(hierarchy['elements'])
    return elements
