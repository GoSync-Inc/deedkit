"""Admission that protects Between and the remote tests, and the resident-memory book of loaded engines.

The rules are checked when a job arrives and again when it leaves the queue to start a phase. Everything
the rules read goes through the injected probe.

An engine is "loaded" while a job runs its phase and, for an engine that declares a warm worker between jobs
(`persistent`, ANYM-683), while it sits idle. An engine whose child exits with the phase leaves the book at
once. A phase that starts cold is charged its full `resident_gb` plus the reserve against the free memory the
probe shows. A phase whose warm engine is already loaded (idle in the book) has its memory spent: it is charged the
reserve only, and it holds nothing back from the free memory others see (live install, 11-02: the second request of a
loaded 4.9 GB vision worker was refused for 4.9 GB more than the probe could show)."""

import time
from dataclasses import dataclass

BUSY_STALE_SECONDS = 30.0


@dataclass
class Verdict:
    action: str  # "go" | "wait" | "evict" | "refuse"
    reason: str | None = None
    evict: tuple = ()


@dataclass
class Loaded:
    key: tuple
    gb: float
    running: int = 0
    last_used: float = 0.0
    engine: object = None
    phase_name: str = ""
    persistent: bool = False


class Admission:
    def __init__(self, config, probe, wall=time.time, monotonic=time.monotonic):
        self.config, self.probe, self.wall, self.monotonic = config, probe, wall, monotonic
        self.loaded = {}
        self.pending = {}  # key -> resident_gb of a running phase, held back from the free memory others see
        self.settle_until = 0.0

    # --- resident book ---------------------------------------------------------------------------------
    def resident_total(self):
        return sum(item.gb for item in self.loaded.values())

    def reconcile(self):
        """Forget an idle persistent worker engine whose worker has died (a crash, a kill that did not come through
        the scheduler): its memory is back and nothing is warm, so the next phase is charged as a cold start."""
        for key, item in list(self.loaded.items()):
            if item.running == 0 and item.persistent and getattr(item.engine, "worker", False):
                worker = item.engine.workers.get(item.phase_name)
                if worker is None or not worker.alive():
                    del self.loaded[key]
                    self.pending.pop(key, None)

    def warm(self, key):
        """The key's engine keeps a worker between jobs and has it loaded now (running or idle)."""
        item = self.loaded.get(key)
        return item is not None and item.persistent

    def mark_running(self, key, gb, engine, phase_name):
        item = self.loaded.get(key)
        warm = item is not None and item.persistent
        if item is None:
            item = self.loaded[key] = Loaded(
                key, gb, engine=engine, phase_name=phase_name, persistent=bool(getattr(engine, "persistent", False)))
        if key not in self.pending:  # a warm engine's memory is already in the free figure: nothing to hold back
            self.pending[key] = 0.0 if warm else gb  # no probe read here: all probing is done before the deadline check
        item.running += 1
        item.last_used = self.monotonic()

    def mark_idle(self, key):
        self.pending.pop(key, None)
        item = self.loaded.get(key)
        if item is None:
            return
        item.running = max(0, item.running - 1)
        item.last_used = self.monotonic()
        if item.running == 0 and not item.persistent:
            del self.loaded[key]

    def evict(self, keys):
        for key in keys:
            self.pending.pop(key, None)
            item = self.loaded.pop(key, None)
            if item is not None and item.engine is not None and item.persistent:
                item.engine.unload(item.phase_name)
        if keys:
            self.settle_until = self.monotonic() + self.config.evict_settle_seconds

    def drop(self, key):
        """A dead child (crash, timeout, kill, cancel): its memory is back at once, nothing to wait for."""
        self.pending.pop(key, None)
        item = self.loaded.pop(key, None)
        if item is not None and item.engine is not None and item.persistent:
            item.engine.unload(item.phase_name)

    def expired_idle(self):
        horizon = self.monotonic() - self.config.keep_alive_seconds
        return tuple(k for k, v in self.loaded.items() if v.running == 0 and v.last_used <= horizon)

    def pending_gb(self, exclude):
        """Memory that running cold starts may take: each one's full resident_gb until the phase ends. Nothing is
        subtracted for a drop of the free memory, because the probe cannot tell the child's allocation from
        another process's: counting it twice only ever makes a second phase wait."""
        return sum(gb for key, gb in self.pending.items() if key != exclude)

    # --- the rules -------------------------------------------------------------------------------------
    def busy_reason(self):
        state = self.probe.read_busy()
        if state.problem:
            return f"busy_file_{state.problem}"
        if abs(self.wall() - state.ts) > BUSY_STALE_SECONDS:
            return "busy_file_stale"
        if state.between:
            return "busy_between"
        if state.deedkit_ci:
            return "busy_deedkit_ci"
        return None

    def load_reason(self, slot):
        cap = self.config.slot_max_load_1m.get(slot, self.config.max_load_1m)
        return "load_high" if self.probe.load_1m() > cap else None

    def check(self, key, resident_gb, slot, *, arrival=False, anything_running=False):
        """The verdict for one phase about to start (or, with `arrival`, for a job that just arrived)."""
        self.reconcile()
        reason = self.busy_reason() or self.load_reason(slot)
        if reason:
            return Verdict("refuse", reason)
        if resident_gb > self.config.resident_cap_gb:
            return Verdict("refuse", "resident_cap")
        if not arrival or not anything_running:
            need = (0.0 if self.warm(key) else resident_gb) + self.config.min_free_gb
            visible = self.probe.available_gb()
            if visible < need:
                if self.monotonic() < self.settle_until:
                    return Verdict("wait")
                return Verdict("refuse", "memory_low")
            if visible - self.pending_gb(key) < need:
                return Verdict("wait", "memory_pending")  # a cold start that is still taking its memory
        if arrival:
            return Verdict("go")
        if key not in self.loaded and self.resident_total() + resident_gb > self.config.resident_cap_gb:
            idle = sorted(
                (v for v in self.loaded.values() if v.running == 0), key=lambda v: v.last_used
            )
            total, victims = self.resident_total(), []
            for item in idle:
                if total + resident_gb <= self.config.resident_cap_gb:
                    break
                total -= item.gb
                victims.append(item.key)
            if total + resident_gb <= self.config.resident_cap_gb:
                return Verdict("evict", "resident_cap", tuple(victims))
            return Verdict("wait", "resident_cap")
        return Verdict("go")

    def overview(self):
        """Open or closed for /health: would a job that needs nothing resident be admitted right now."""
        if self.busy_reason() or (self.load_reason("gpu") and self.load_reason("cpu")):
            return "closed"
        return "open" if self.probe.available_gb() >= self.config.min_free_gb else "closed"
