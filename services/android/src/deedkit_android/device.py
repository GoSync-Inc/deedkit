"""One process and one reservation at a time per physical device."""

import asyncio
import base64
from contextlib import asynccontextmanager, nullcontext
import os
from pathlib import Path
import re
import signal
import shlex
import struct
import io

from PIL import Image

import yaml
import tempfile
import time
import uuid
from xml.etree import ElementTree

from .config import Refused
from .uiautomator import DEFINITE, Uiautomator, UiRunner
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
        self.ui = Uiautomator(config, store.lock_fd) if getattr(config, "uiautomator2_jar", None) else None

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.ui:
            await self.ui.close()

    @asynccontextmanager
    async def locked(self, owner, device, session):
        self.store.touch(owner, device, session)
        if self.ui:
            await self.ui.ensure_fenced_process_stopped(device)
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

    async def observe(self, owner, device, session, screenshot=True, hierarchy=True, image_mode="inline", crop=None):
        async with self.locked(owner, device, session):
            result = {"device_id": device, "observed_at": time.time(),
                      "coordinate_space": "device_pixels", "resized": False}
            jobs = []
            if screenshot:
                jobs.append(self.capture_image(owner, device, image_mode, crop))
            if hierarchy:
                jobs.append(self.capture_hierarchy(device))
            tasks = [asyncio.create_task(job) for job in jobs]
            try:
                for part in await asyncio.gather(*tasks):
                    result.update(part)
                return result
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def capture_image(self, owner, device, image_mode, crop):
        try:
            code, data, _ = await run_process([*self.adb(device), "exec-out", "screencap", "-p"],
                                              6, stdout_limit=SCREENSHOT_LIMIT)
            if code == 0 and data.startswith(b"\x89PNG\r\n\x1a\n"):
                metadata = {}
                if len(data) >= 24 and data[12:16] == b"IHDR":
                    width, height = struct.unpack(">II", data[16:24])
                    metadata = {"width": width, "height": height}
                result = {"device_size": dict(metadata)}
                if crop:
                    if not metadata or crop["x"] + crop["w"] > width or crop["y"] + crop["h"] > height:
                        raise Refused("crop_outside_screen", 422)
                    with Image.open(io.BytesIO(data)) as original:
                        frame = original.crop((crop["x"], crop["y"], crop["x"] + crop["w"], crop["y"] + crop["h"]))
                        target = io.BytesIO()
                        frame.save(target, format="PNG", compress_level=1)
                        data = target.getvalue()
                    metadata = {"width": crop["w"], "height": crop["h"]}
                    result["image_origin"] = {"x": crop["x"], "y": crop["y"]}
                else:
                    result["image_origin"] = {"x": 0, "y": 0}
                if len(data) > SCREENSHOT_LIMIT:
                    raise Refused("screenshot_too_large", 413)
                if image_mode in {"inline", "both"}:
                    result["image"] = {"mime_type": "image/png", "data": base64.b64encode(data).decode(), **metadata}
                if image_mode in {"file", "both"}:
                    result["image_file"] = {**self.store.save(owner, data), "mime_type": "image/png", **metadata}
                return result
        except (TimeoutError, OSError):
            pass
        return {"screenshot_error": "unavailable"}

    async def capture_hierarchy(self, device):
        if self.ui:
            try:
                async with asyncio.timeout(8):
                    result = await self.ui.call(device, '__hierarchy__')
                root = ElementTree.fromstring(result['xml'])
                elements = []
                for node in root.iter('node'):
                    item = dict(node.attrib)
                    if item.get('password') == 'true':
                        item['text'], item['content-desc'] = '', ''
                    elements.append(item)
                return {'hierarchy_source': 'uiautomator2', 'elements': elements}
            except (TimeoutError, OSError, Refused, ValueError, KeyError):
                return {'hierarchy_source': 'uiautomator2', 'hierarchy_error': 'unavailable'}
        path = "/data/local/tmp/deedkit-" + uuid.uuid4().hex + ".xml"
        script = f"uiautomator dump {path} >/dev/null && cat {path}; result=$?; rm -f {path}; exit $result"
        try:
            code, data, stderr = await run_process([*self.adb(device), "shell", script], 8)
            root = ElementTree.fromstring(data[data.index(b"<?xml"):])
            elements = []
            for node in root.iter("node"):
                item = dict(node.attrib)
                if item.get("password") == "true":
                    item["text"], item["content-desc"] = "", ""
                elements.append(item)
            return {"elements": elements, "hierarchy_source": "adb"}
        except (TimeoutError, OSError, Refused):
            return {"hierarchy_error": "capture_failed"}
        except (ValueError, ElementTree.ParseError):
            return {"hierarchy_error": "unavailable", "hierarchy_exit_code": code,
                    "hierarchy_diagnostic": stderr.decode(errors="replace")[:1000]}

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

    async def execute(self, owner, device, session, request_id, argv, timeout, wait, flow=None,
                      element_timeout_ms=5000, optional_timeout_ms=1000, identity=None, metadata=None):
        # A known request is read before reservation checks: its receipt survives lease expiry.
        payload = {"device_id": device, "timeout_s": timeout}
        payload["flow" if flow is not None else "argv"] = flow if flow is not None else argv
        # Keep the default payload hash compatible with already persisted receipts.
        if flow is not None and (element_timeout_ms, optional_timeout_ms) != (5000, 1000):
            payload.update(element_timeout_ms=element_timeout_ms, optional_timeout_ms=optional_timeout_ms)
        try:
            previous = self.store.operation(owner, request_id)
        except Refused as error:
            if error.status != 404:
                raise
        else:
            self.store.admit(owner, request_id, payload)
            return previous
        self.store.touch(owner, device, session)
        if self.ui:
            await self.ui.ensure_fenced_process_stopped(device)
        lock = self.locks[device]
        if lock.locked():
            raise Refused("device_busy")
        await lock.acquire()
        temporary = None
        try:
            temporary = tempfile.TemporaryDirectory(dir=self.store.directory)
            if flow is None:
                command, output = self.prepare(owner, device, argv, Path(temporary.name))
            elif not self.ui:
                raise Refused("ui_engine_not_configured", 503)
            self.store.admit(owner, request_id, payload, identity=identity, metadata=metadata)
        except BaseException:
            if temporary is not None:
                temporary.cleanup()
            lock.release()
            raise
        if flow is None:
            operation = self.perform(owner, request_id, command, output, timeout, temporary, lock)
        else:
            operation = self.perform_flow(owner, device, request_id, flow, timeout, temporary, lock,
                                          element_timeout_ms, optional_timeout_ms)
        task = asyncio.create_task(self.keep_reserved(owner, device, session, operation))
        self.tasks[(owner, request_id)] = task
        task.add_done_callback(lambda _: self.tasks.pop((owner, request_id), None))
        return await self.receipt(owner, request_id, wait)

    async def keep_reserved(self, owner, device, session, operation):
        async def renew():
            while True:
                await asyncio.sleep(5)
                self.store.touch(owner, device, session)
        heartbeat = asyncio.create_task(renew())
        try:
            return await operation
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            # Leave a full inactivity interval after completion for the next agent decision.
            try:
                self.store.touch(owner, device, session)
            except Refused:
                pass  # The caller may have released the completed operation already.

    async def failure_observation(self, owner, device):
        """Capture while holding the operation lock; store once, never at poll time."""
        result = {"device_id": device, "observed_at": time.time(),
                  "coordinate_space": "device_pixels", "resized": False}

        async def capture(kind, operation):
            try:
                async with asyncio.timeout(8):
                    return await operation
            except Exception:
                # A protected screen, full file quota or offline driver must not
                # replace the operation's original failure or leak its lock.
                return {kind + '_error': 'unavailable'}

        image, hierarchy = await asyncio.gather(
            capture('screenshot', self.capture_image(owner, device, 'file', None)),
            capture('hierarchy', self.capture_hierarchy(device)))
        elements = hierarchy.pop('elements', [])
        if elements:
            hierarchy['elements'] = [{key: value[:256] if isinstance(value, str) else value
                                      for key, value in item.items() if key in {
                                          'text', 'resource-id', 'content-desc', 'bounds', 'class',
                                          'clickable', 'enabled', 'focused', 'checked', 'scrollable'}}
                                     for item in elements[:80]]
            hierarchy['element_count'] = len(elements)
            hierarchy['truncated'] = len(elements) > 80
        result.update(image)
        result.update(hierarchy)
        return result

    def remaining(self, commands):
        from .app_maps import continuation
        return continuation(commands)

    async def perform_flow(self, owner, device, request_id, commands, timeout, temporary, lock,
                           element_timeout_ms=5000, optional_timeout_ms=1000):
        state, result = "unknown", {}
        started = time.monotonic()
        runner = UiRunner(lambda command, **options: self.ui.call(device, command, **options),
                          element_timeout_ms, optional_timeout_ms)
        cancelled = False
        try:
            checkpoint = next((i for i, command in enumerate(commands)
                               if isinstance(command, dict) and "checkpoint" in command), len(commands))
            if checkpoint:
                async with asyncio.timeout(timeout):
                    await runner.run(commands[:checkpoint])
            result = {"commands_executed": checkpoint, "device_id": device}
            state = "succeeded"
            if checkpoint < len(commands):
                state = "paused"
                result.update({"checkpoint": commands[checkpoint]["checkpoint"],
                               "remaining_yaml": yaml.safe_dump(self.remaining(commands[checkpoint + 1:]), allow_unicode=True),
                               "next_action": "observe_then_resolve_checkpoint_and_submit_remaining_yaml"})
        except Refused as error:
            result = {"error": error.code, "next_action": "inspect_device_before_any_new_request"}
            if error.code in DEFINITE:
                state = 'failed'
                result['next_action'] = 'inspect_failure_observation_and_correct_flow'
            if hasattr(error, 'command_path'):
                result.update(failed_command_index=error.command_path[0],
                              failed_command_path=error.command_path, failed_command=error.command)
            if getattr(error, "details", None):
                result["details"] = error.details
            if getattr(error, "hint", None):
                result["hint"] = error.hint
            if getattr(error, "diagnostic", None):
                result["diagnostic"] = error.diagnostic
        except TimeoutError:
            result = {"error": "command_timeout", "next_action": "inspect_device_before_any_new_request"}
        except asyncio.CancelledError:
            cancelled = True
            result = {"error": "service_stopped", "next_action": "inspect_device_before_any_new_request"}
        except (OSError, ValueError):
            result = {"error": "ui_engine_unavailable", "next_action": "inspect_device_before_any_new_request"}
        finally:
            try:
                if state not in {'succeeded', 'paused'}:
                    result.update(device_id=device, commands_executed=runner.completed,
                                  automatic_retry=False, side_effects='may_have_occurred')
                    if runner.current_path is not None and 'failed_command_path' not in result:
                        result.update(failed_command_index=runner.current_path[0],
                                      failed_command_path=runner.current_path, failed_command=runner.current_command)
                    if not cancelled:
                        result['observation'] = await self.failure_observation(owner, device)
                result["data"] = {step["result"]["list"]: step["result"]["rows"] for step in runner.steps
                                  if "result" in step and "rows" in step["result"]}
                result["data"].update({step["result"]["key"]: step["result"]["lines"] for step in runner.steps
                                       if "result" in step and "lines" in step["result"]})
                result.update(engine='uiautomator2', steps=runner.steps,
                              elapsed_ms=round((time.monotonic() - started) * 1000, 2))
                self.store.finish(owner, request_id, state, result)
            finally:
                temporary.cleanup()
                lock.release()

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
            if getattr(error, "diagnostic", None):
                result["diagnostic"] = error.diagnostic
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
