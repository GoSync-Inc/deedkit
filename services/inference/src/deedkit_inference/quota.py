"""Daily job counts (UTC day, per accepted job) and concurrency, per principal. Metadata only."""

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

LOG = logging.getLogger("deedkit_inference.quota")


def today():
    return datetime.now(UTC).date().isoformat()


class Quota:
    def __init__(self, usage_file=None):
        self.path = Path(usage_file) if usage_file else None
        self.day, self.counts = today(), {}
        self.inflight = {}
        if self.path:
            self._load()

    def _load(self):
        try:
            data = json.loads(self.path.read_text())
            if data.get("day") == today() and isinstance(data.get("counts"), dict):
                self.counts = {str(k): int(v) for k, v in data["counts"].items()}
        except (OSError, ValueError, TypeError, AttributeError):
            self.counts = {}

    def _roll(self):
        if self.day != today():
            self.day, self.counts = today(), {}

    def used_today(self, principal_id):
        self._roll()
        return self.counts.get(principal_id, 0)

    def daily_exhausted(self, principal):
        return self.used_today(principal.id) >= principal.max_jobs_per_day

    def concurrency_exhausted(self, principal):
        return self.inflight.get(principal.id, 0) >= principal.max_concurrent

    def enter(self, principal_id):
        self.inflight[principal_id] = self.inflight.get(principal_id, 0) + 1

    def leave(self, principal_id):
        self.inflight[principal_id] = max(0, self.inflight.get(principal_id, 0) - 1)

    def accept(self, principal):
        """Count one accepted job; False when the cap is already reached (checked and counted together)."""
        if self.daily_exhausted(principal):
            return False
        self.counts[principal.id] = self.used_today(principal.id) + 1
        self._save()
        return True

    def _save(self):
        if not self.path:
            return
        temp = Path(f"{self.path}.{os.getpid()}.tmp")
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump({"day": self.day, "counts": self.counts}, handle)
            os.replace(temp, self.path)
        except OSError:
            LOG.warning("usage file not written code=usage_unwritable")
