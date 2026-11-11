"""Control flow (runFlow conditions, repeat, retry) shared by the uiautomator2 batch runner.

Each command and read-only probe goes through the engine's call; the base class
only walks nested commands, records paths and never repeats a mutation itself.
"""

import asyncio
import time

from .config import Refused


class FlowRunner:
    def __init__(self, call, element_timeout_ms, optional_timeout_ms):
        self.call = call
        self.element_timeout_ms, self.optional_timeout_ms = element_timeout_ms, optional_timeout_ms
        self.completed = 0
        self.current_path, self.current_command = None, None

    async def probe(self, selector, timeout, visible=True):
        raise NotImplementedError

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
        name, argument = next(iter(command.items()))
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
        elif name == 'retry':
            # An explicit retry block, capped at three retries. Only a definite
            # lookup miss is retried; an uncertain failure stops the batch.
            limit = min(int(argument.get('maxRetries', 1)), 3)
            for attempt in range(limit + 1):
                try:
                    await self.run(argument['commands'], path)
                    break
                except Refused as error:
                    if error.code != 'element_not_found' or attempt == limit:
                        raise
        else:
            raise Refused('unsupported_uiautomator_command', 422)


class Progress:
    """Step-boundary progress of one running flow (ANYM-970).

    `write(step_path, step_name, finished_steps, elapsed_ms)` gets the latest boundary at most every `interval` seconds:
    at once when the last write is older, else from a timer, so a slow step shows within one interval. It is advisory:
    a failing write is dropped and never reaches the run."""

    def __init__(self, write, started, interval=0.25, clock=time.monotonic):
        self.write, self.started, self.interval, self.clock = write, started, interval, clock
        self.pending, self.last, self.timer = None, None, None

    def step(self, path, name, finished):
        now = self.clock()
        self.pending = (path, name, finished, round((now - self.started) * 1000, 2))
        if self.last is None or now - self.last >= self.interval:
            self.flush()
        elif self.timer is None:
            self.timer = asyncio.get_running_loop().call_later(self.last + self.interval - now, self.flush)

    def flush(self):
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        if self.pending is None:
            return
        pending, self.pending, self.last = self.pending, None, self.clock()
        try:
            self.write(*pending)
        except Exception:  # noqa: BLE001 - progress is advisory; the receipt at the end is the record
            pass

    def close(self):
        """Stop writing, before the final receipt is stored."""
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        self.pending = None
