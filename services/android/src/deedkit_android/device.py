"""One process and one reservation at a time per physical device."""

import asyncio
import base64
from contextlib import asynccontextmanager, nullcontext
import os
from pathlib import Path
import re
import signal
import shlex
import tempfile
import time
import uuid
from xml.etree import ElementTree

from .config import Refused
from .store import MAX_FILE

OUTPUT_LIMIT = 4 * 1024 * 1024
SCREENSHOT_LIMIT = 16 * 1024 * 1024
COMMANDS = frozenset({
    "shell", "exec-out", "reboot", "logcat", "get-state", "get-devpath", "wait-for-device",
    "root", "unroot", "remount", "disable-verity", "enable-verity", "reconnect", "jdwp",
    "push", "pull", "install", "install-multiple", "uninstall",
})
INSTALL_FLAGS = frozenset({
    "-r", "-t", "-d", "-g", "-s", "-f", "-p", "--instant", "--full", "--dont-kill",
    "--enable-rollback", "--force-queryable", "--no-streaming", "--streaming", "--no-incremental",
    "--staged", "--force-non-staged",
})
INSTALL_VALUE_FLAGS = frozenset({"-i", "--abi", "--user", "--install-location", "--install-reason"})


async def stop_process(process):
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        await asyncio.wait_for(process.wait(), 2)
    except TimeoutError:
        os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
    except ProcessLookupError:
        await process.wait()


async def run_process(argv, timeout, output_file=None, stdout_limit=OUTPUT_LIMIT):
    async def read(stream, sink=None):
        result = bytearray()
        size = 0
        while chunk := await stream.read(65536):
            size += len(chunk)
            limit = MAX_FILE if sink else stdout_limit if stream is process.stdout else OUTPUT_LIMIT
            if size > limit:
                raise Refused("output_limit_reached")
            if sink:
                sink.write(chunk)
            else:
                result.extend(chunk)
        return bytes(result)

    with output_file.open("wb") if output_file else nullcontext(None) as sink:
        process = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, start_new_session=True,
        )
        combined = asyncio.gather(read(process.stdout, sink), read(process.stderr), process.wait())
        try:
            async with asyncio.timeout(timeout):
                stdout, stderr, code = await combined
                return code, stdout, stderr
        finally:
            await stop_process(process)
            combined.cancel()
            await asyncio.gather(combined, return_exceptions=True)


class Devices:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.locks = {name: asyncio.Lock() for name in config.devices}
        self.tasks = {}

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @asynccontextmanager
    async def locked(self, owner, device, session):
        self.store.check_reservation(owner, device, session)
        lock = self.locks[device]
        if lock.locked():
            raise Refused("device_busy")
        await lock.acquire()
        try:
            yield
        finally:
            lock.release()

    def adb(self, device):
        return [self.config.adb, "-s", self.config.devices[device]]

    async def status(self, allowed):
        try:
            code, stdout, _ = await run_process([self.config.adb, "devices", "-l"], 1.5)
        except (TimeoutError, OSError, Refused):
            return [{"device_id": name, "state": "unavailable"} for name in allowed]
        states = {}
        if code == 0:
            for line in stdout.decode(errors="replace").splitlines()[1:]:
                words = line.split()
                if len(words) > 1:
                    states[words[0]] = words[1]
        return [{"device_id": name, "state": states.get(self.config.devices[name], "disconnected")}
                for name in allowed]

    async def observe(self, owner, device, session, screenshot=True, hierarchy=True):
        async with self.locked(owner, device, session):
            result = {"device_id": device, "observed_at": time.time()}
            if hierarchy:
                path = "/data/local/tmp/deedkit-" + uuid.uuid4().hex + ".xml"
                script = f"uiautomator dump {path} >/dev/null && cat {path}; result=$?; rm -f {path}; exit $result"
                try:
                    code, data, stderr = await run_process([*self.adb(device), "shell", script], 15)
                    start = data.index(b"<?xml")
                    root = ElementTree.fromstring(data[start:])
                    result["elements"] = []
                    for node in root.iter("node"):
                        item = dict(node.attrib)
                        if item.get("password") == "true":
                            item["text"], item["content-desc"] = "", ""
                        result["elements"].append(item)
                except (TimeoutError, OSError, Refused):
                    result["hierarchy_error"] = "capture_failed"
                except (ValueError, ElementTree.ParseError):
                    result["hierarchy_error"] = "unavailable"
                    result["hierarchy_exit_code"] = code
                    result["hierarchy_diagnostic"] = stderr.decode(errors="replace")[:1000]
            if screenshot:
                try:
                    code, data, _ = await run_process([*self.adb(device), "exec-out", "screencap", "-p"],
                                                      10, stdout_limit=SCREENSHOT_LIMIT)
                except (TimeoutError, OSError, Refused):
                    code, data = -1, b""
                if code == 0 and data.startswith(b"\x89PNG\r\n\x1a\n"):
                    result["image"] = {"mime_type": "image/png", "data": base64.b64encode(data).decode()}
                else:
                    result["screenshot_error"] = "unavailable"
            return result

    def prepare(self, owner, device, argv, temporary):
        if not argv or argv[0] not in COMMANDS:
            raise Refused("unsupported_adb_command", 422)
        if argv[0] in {"wait-for-device", "reconnect"} and len(argv) != 1:
            raise Refused("command_takes_no_arguments", 422)
        argv = list(argv)
        output = None
        if argv[0] == "push":
            if len(argv) != 3:
                raise Refused("push_takes_file_id_and_device_path", 422)
            argv[1] = str(self.store.file(owner, argv[1]))
        elif argv[0] == "pull":
            if len(argv) != 2 or argv[1].startswith("-"):
                raise Refused("pull_takes_one_device_path", 422)
            output = temporary / "download"
            argv = ["exec-out", "cat", "--", shlex.quote(argv[1])]
        elif argv[0] in {"install", "install-multiple"}:
            index = 1
            while index < len(argv) and argv[index].startswith("-"):
                flag = argv[index]
                index += 1
                if flag in INSTALL_VALUE_FLAGS:
                    if index == len(argv) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", argv[index]):
                        raise Refused("invalid_install_option_value", 422)
                    # ADB also discovers APK operands by suffix, including option values.
                    if argv[index].lower().endswith((".apk", ".apex")):
                        raise Refused("invalid_install_option_value", 422)
                    index += 1
                elif flag not in INSTALL_FLAGS:
                    raise Refused("unsupported_install_option", 422)
            if index == len(argv) or (argv[0] == "install" and len(argv) - index != 1):
                raise Refused("install_requires_uploaded_file", 422)
            files = [self.store.file(owner, file_id) for file_id in argv[index:]]
            for offset, source in enumerate(files):
                target = temporary / f"{offset}.apk"
                os.link(source, target)
                argv[index + offset] = str(target)
        return [*self.adb(device), *argv], output

    async def execute(self, owner, device, session, request_id, argv, timeout, wait):
        # A known request is read before reservation checks: its receipt survives lease expiry.
        payload = {"device_id": device, "argv": argv, "timeout_s": timeout}
        try:
            previous = self.store.operation(owner, request_id)
        except Refused as error:
            if error.status != 404:
                raise
        else:
            self.store.admit(owner, request_id, payload)
            return previous
        self.store.check_reservation(owner, device, session)
        lock = self.locks[device]
        if lock.locked():
            raise Refused("device_busy")
        await lock.acquire()
        temporary = None
        try:
            temporary = tempfile.TemporaryDirectory(dir=self.store.directory)
            command, output = self.prepare(owner, device, argv, Path(temporary.name))
            self.store.admit(owner, request_id, payload)
        except BaseException:
            if temporary is not None:
                temporary.cleanup()
            lock.release()
            raise
        task = asyncio.create_task(self.perform(owner, request_id, command, output, timeout, temporary, lock))
        self.tasks[(owner, request_id)] = task
        task.add_done_callback(lambda _: self.tasks.pop((owner, request_id), None))
        return await self.receipt(owner, request_id, wait)

    async def perform(self, owner, request_id, command, output, timeout, temporary, lock):
        state, result = "unknown", {}
        try:
            code, stdout, stderr = await run_process(command, timeout, output)
            result = {"exit_code": code, "stderr": stderr.decode(errors="replace")[:16384]}
            try:
                text = stdout.decode("utf-8")
                if len(text) <= 32768 and "\x00" not in text:
                    result["stdout"] = text
                else:
                    result["stdout_file"] = self.store.save(owner, stdout)
            except UnicodeDecodeError:
                result["stdout_file"] = self.store.save(owner, stdout)
            if output and output.is_file():
                result["download"] = self.store.save_path(owner, output)
            state = "succeeded" if code == 0 else "failed"
        except TimeoutError:
            result = {"error": "command_timeout", "next_action": "inspect_device_before_any_new_request"}
        except Refused as error:
            result = {"error": error.code, "next_action": "inspect_device_before_any_new_request"}
        except asyncio.CancelledError:
            result = {"error": "service_stopped", "next_action": "inspect_device_before_any_new_request"}
        except OSError:
            result = {"error": "process_failed", "next_action": "inspect_device_before_any_new_request"}
        finally:
            try:
                self.store.finish(owner, request_id, state, result)
            finally:
                temporary.cleanup()
                lock.release()

    async def receipt(self, owner, request_id, wait):
        task = self.tasks.get((owner, request_id))
        if task and wait:
            try:
                await asyncio.wait_for(asyncio.shield(task), wait)
            except TimeoutError:
                pass
        return self.store.operation(owner, request_id)
