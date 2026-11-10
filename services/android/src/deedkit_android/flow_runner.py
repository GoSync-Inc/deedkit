"""Bounded element probes around native Maestro commands, on the existing MCP session.

Maestro 2.12's tap and conditional lookups have hardcoded waits. Native
extendedWaitUntil has an explicit timeout, so use that read-only probe before
an action. Never time out an optional mutation and pretend it was skipped.
"""

import yaml

from .config import Refused

LOOKUPS = {'tapOn', 'doubleTapOn', 'longPressOn', 'assertVisible', 'assertNotVisible', 'copyTextFrom'}
MATCH_FIELDS = {'text', 'id', 'index', 'enabled', 'selected', 'checked', 'focused', 'width', 'height',
                'below', 'above', 'leftOf', 'rightOf', 'containsChild', 'containsDescendants',
                'childOf', 'traits', 'tolerance'}


class FlowRunner:
    def __init__(self, call, serial, element_timeout_ms, optional_timeout_ms):
        self.call, self.serial = call, serial
        self.element_timeout_ms, self.optional_timeout_ms = element_timeout_ms, optional_timeout_ms
        self.completed = 0
        self.current_path, self.current_command = None, None

    async def native(self, command):
        return await self.call('run', {'device_id': self.serial,
            'yaml': 'appId: android\n---\n' + yaml.safe_dump([command], allow_unicode=True)}, 600)

    async def probe(self, selector, timeout, visible=True):
        try:
            await self.native({'extendedWaitUntil': {
                'visible' if visible else 'notVisible': selector, 'timeout': timeout}})
            return True
        except Refused as error:
            # Only this native assertion means a completed read-only miss. Transport,
            # parser and driver errors must not turn an optional step into success.
            if (error.code == 'maestro_action_failed' and
                    getattr(error, 'diagnostic', '').startswith('Failed to run flow: Assertion is false:')):
                return False
            raise

    async def condition(self, condition):
        if condition.get('platform', 'android').lower() != 'android':
            return False
        timeout = condition.get('timeout', self.optional_timeout_ms)
        for field in ('visible', 'notVisible'):
            if field in condition and not await self.probe(condition[field], timeout, field == 'visible'):
                return False
        return True

    async def run(self, commands, parent=()):
        for index, command in enumerate(commands):
            path = [*parent, index]
            self.current_path, self.current_command = path, command
            try:
                await self.step(command, path)
            except Refused as error:
                if not hasattr(error, 'command_path'):
                    error.command_path, error.command = path, command
                raise
            if not parent:
                self.completed = index + 1

    async def step(self, command, path):
        if not isinstance(command, dict):
            await self.native(command)
            return
        name, argument = next(iter(command.items()))
        if name in {'runFlow', 'repeat', 'retry'}:
            if name == 'runFlow':
                if await self.condition(argument.get('when', {})):
                    await self.run(argument['commands'], path)
            elif name == 'repeat':
                count = int(float(argument['times'])) if 'times' in argument else None
                iteration = 0
                while ((count is None or iteration < count) and
                       await self.condition(argument.get('while', {}))):
                    await self.run(argument['commands'], path)
                    iteration += 1
            else:
                # Match Maestro's explicit retry block, including its three-retry cap.
                for attempt in range(min(int(argument.get('maxRetries', 1)), 3) + 1):
                    try:
                        await self.run(argument['commands'], path)
                        break
                    except Refused as error:
                        if (error.code not in {'element_not_found', 'maestro_action_failed'} or
                                attempt == min(int(argument.get('maxRetries', 1)), 3)):
                            raise
            return
        if name in LOOKUPS:
            options = argument if isinstance(argument, dict) else {'text': argument}
            selector = {key: value for key, value in options.items() if key in MATCH_FIELDS}
            # A point-only tap has no element lookup. Relative point taps still probe
            # their selector; Maestro performs the actual tap with all original options.
            if selector:
                optional = options.get('optional', False)
                timeout = options.get('timeout', self.optional_timeout_ms if optional else self.element_timeout_ms)
                found = await self.probe(selector, timeout, name != 'assertNotVisible')
                if not found:
                    if optional:
                        return
                    error = Refused('element_not_found')
                    error.diagnostic = 'Element condition not met within ' + str(timeout) + ' ms.'
                    raise error
                if name in {'assertVisible', 'assertNotVisible'}:
                    return
            if isinstance(argument, dict):
                command = {name: {key: value for key, value in argument.items() if key != 'timeout'}}
        await self.native(command)
