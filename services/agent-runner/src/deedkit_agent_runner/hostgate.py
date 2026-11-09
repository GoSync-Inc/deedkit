"""The host gate and the startup self-probe are the host's (S3, ANYM-821): the runner runs the configured command
and reads one JSON line. Anything but a clean `ok` within the timeout reads busy (gate) or failed (probe)."""

import json
import subprocess

REASONS = ("memory", "disk", "inference", "tests")


def _read(command):
    try:
        done = subprocess.run(command.argv, capture_output=True, timeout=command.timeout_s, check=False,
                              stdin=subprocess.DEVNULL)
        value = json.loads(done.stdout.decode().strip().splitlines()[-1]) if done.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        value = None
    return value if isinstance(value, dict) else None


class CommandGate:
    def __init__(self, gate, probe):
        self.gate, self.probe = gate, probe

    def read(self):
        """('ok', None) or ('busy', reason)."""
        value = _read(self.gate)
        if value and value.get("state") == "ok":
            return "ok", None
        reason = value.get("reason") if value else None
        return "busy", reason if reason in REASONS else "unknown"

    def self_probe(self):
        value = _read(self.probe)
        return "ok" if value and value.get("state") == "ok" else "failed"
