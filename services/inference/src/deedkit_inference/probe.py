"""One injectable probe for memory, load, the busy file and child sizes (no macOS needed in tests)."""

import json
import math
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class BusyState:
    """The parsed busy file. `problem` is None, "missing" or "invalid"; fail closed on anything else."""

    ts: float | None = None
    between: bool = False
    deedkit_ci: bool = False
    problem: str | None = None


class Probe(Protocol):
    def available_gb(self) -> float: ...
    def load_1m(self) -> float: ...
    def read_busy(self) -> BusyState: ...
    def rss_gb(self, pgid: int) -> float: ...


def parse_busy(raw):
    try:
        value = json.loads(raw)
        ts, between, ci = value["ts"], value["between"], value["deedkit_ci"]
        if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not math.isfinite(ts):
            raise ValueError
        if not isinstance(between, bool):
            raise ValueError
        if not isinstance(ci, bool):
            raise ValueError
        return BusyState(ts=float(ts), between=between, deedkit_ci=ci)
    except (ValueError, KeyError, TypeError):
        return BusyState(problem="invalid")


def read_busy_file(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return BusyState(problem="missing")
    except OSError:
        return BusyState(problem="invalid")
    try:
        return parse_busy(os.read(fd, 4096))
    except OSError:
        return BusyState(problem="invalid")
    finally:
        os.close(fd)


class SystemProbe:
    """The real host. Figures are cached for a second so a burst of checks costs one `vm_stat`."""

    def __init__(self, busy_file, ttl=1.0):
        self.busy_file, self.ttl = busy_file, ttl
        self._memory = (0.0, 0.0)

    def available_gb(self):
        now = time.monotonic()
        if now - self._memory[0] > self.ttl:
            self._memory = (now, self._read_available())
        return self._memory[1]

    @staticmethod
    def _read_available():
        """Free plus inactive memory, the figure scripts/remote-test.sh uses. Unknown means zero (fail closed)."""
        try:
            if sys.platform == "darwin":
                text = subprocess.run(["/usr/bin/vm_stat"], capture_output=True, text=True, timeout=2).stdout
                page = int(re.search(r"page size of (\d+) bytes", text).group(1))
                pages = sum(int(m.group(1)) for m in re.finditer(r"Pages (?:free|inactive):\s+(\d+)", text))
                return pages * page / 1024**3
            with open("/proc/meminfo") as handle:
                text = handle.read()
            return int(re.search(r"MemAvailable:\s+(\d+) kB", text).group(1)) / 1024**2
        except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
            return 0.0

    def load_1m(self):
        try:
            return os.getloadavg()[0]
        except OSError:
            return float("inf")

    def read_busy(self):
        return read_busy_file(self.busy_file)

    def rss_gb(self, pgid):
        """Resident size of every process in the process group; zero when it is gone."""
        try:
            text = subprocess.run(
                ["/bin/ps", "-axo", "pgid=,rss="], capture_output=True, text=True, timeout=2
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return 0.0
        total = 0
        for line in text.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == str(pgid) and parts[1].isdigit():
                total += int(parts[1])
        return total / 1024**2


class StaticProbe(SystemProbe):
    """Test-mode figures from the config, so a subprocess test needs no macOS."""

    def __init__(self, busy_file, available_gb, load_1m):
        super().__init__(busy_file)
        self._available, self._load = available_gb, load_1m

    def available_gb(self):
        return self._available

    def load_1m(self):
        return self._load

    def rss_gb(self, pgid):
        return 0.0
