"""Warm engine workers: one child process per loaded `<alias>/<phase>`, in its own process group.

The child (engines/worker.py) loads its model from verified local paths, answers `{"op": "job"}` lines with one JSON
line each and exits on `{"op": "unload"}` or when its stdin closes. The parent kills the group on a timeout, a
memory overrun, a failed job and on shutdown. Each worker has a scratch directory under the work directory that
HOME, TMPDIR and every library cache point at; it goes away with the worker."""

import asyncio
import contextlib
import json
import os
import shutil
import signal

UNLOAD_GRACE_SECONDS = 3.0
MAX_LINE_BYTES = 8 * 1024 * 1024
_REAPERS = set()


def kill_group(pgid):
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


class WorkerFailed(Exception):
    """The worker died, printed garbage, or printed more than the cap."""


class Worker:
    def __init__(self, proc, scratch, key, resident_gb):
        self.proc, self.scratch, self.key, self.resident_gb = proc, scratch, key, resident_gb
        self.job = None  # the Job it is running now, so a memory kill can name its reason
        self.stopped = False
        self.cold = True

    @classmethod
    async def spawn(cls, argv, env, scratch, key, resident_gb):
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            cwd=scratch, env=env, start_new_session=True, limit=MAX_LINE_BYTES)
        return cls(proc, scratch, key, resident_gb)

    def alive(self):
        return not self.stopped and self.proc.returncode is None

    async def line(self):
        try:
            data = await self.proc.stdout.readline()
        except ValueError:  # a line longer than the cap
            self.kill()
            raise WorkerFailed("line_too_long") from None
        if not data or not data.endswith(b"\n"):
            raise WorkerFailed("worker_exited")
        return data

    async def ready(self):
        try:
            if json.loads(await self.line()) != {"ready": True}:
                raise WorkerFailed("not_ready")
        except ValueError:
            raise WorkerFailed("not_ready") from None

    async def call(self, message):
        """Send one job line, return the worker's one answer line."""
        try:
            self.proc.stdin.write(json.dumps(message).encode() + b"\n")
            await self.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            raise WorkerFailed("worker_exited") from None
        self.cold = False
        return await self.line()

    def kill(self):
        """At once: the group dies and the scratch directory goes."""
        self.stopped = True
        kill_group(self.proc.pid)
        shutil.rmtree(self.scratch, ignore_errors=True)

    def stop(self):
        """Ask the worker to clear its caches and exit; the group is killed if it is still there after the grace."""
        if self.stopped:
            return
        self.stopped = True
        with contextlib.suppress(Exception):
            self.proc.stdin.write(b'{"op": "unload"}\n')
            self.proc.stdin.close()
        try:
            task = asyncio.get_running_loop().create_task(self._reap())
        except RuntimeError:
            kill_group(self.proc.pid)
            shutil.rmtree(self.scratch, ignore_errors=True)
            return
        _REAPERS.add(task)
        task.add_done_callback(_REAPERS.discard)

    async def _reap(self):
        try:
            await asyncio.wait_for(self.proc.wait(), UNLOAD_GRACE_SECONDS)
        except TimeoutError:
            pass
        finally:
            kill_group(self.proc.pid)
            with contextlib.suppress(Exception):
                await asyncio.shield(self.proc.wait())
            shutil.rmtree(self.scratch, ignore_errors=True)
