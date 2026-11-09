"""ANYM-896: session holders restart at most once a night, inside the window; a failed night recreate rolls back."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import stat
import time

import pytest

from scripts import production as p
from test_production import OLD_IMAGE, KeepHost

from messaging_service import whatsapp_holder

WITA = timezone(timedelta(hours=8))
SHAS = tuple(str(digit) * 40 for digit in range(1, 8))
HOLDER = "whatsapp1-holder"
COLLECTOR = "whatsapp1"
HOLDER_DOCKERFILE = """FROM golang:1@sha256:{go} AS build
COPY . ./
RUN go build -o /out/anymsg-whatsmeow .
FROM scratch AS whatsapp-holder
COPY --from=build /out/anymsg-whatsmeow /usr/local/bin/anymsg-whatsmeow
"""


@pytest.fixture(autouse=True)
def no_host_state(tmp_path, monkeypatch):
    """As in test_production: no test reads this machine's production state or sleeps through apply's settle."""
    for name in ("HOST_STATE", "CONFIG_DIRECTORY", "CONTAINERD_ROOT", "TOOL_ROOT", "SYSTEMD_DIRECTORY", "BACKUP_RUNNER",
                 "BACKUP_LOCK", "INFERENCE_WORKER_RECEIPT"):
        monkeypatch.setattr(p, name, tmp_path / "absent" / name.lower())
    monkeypatch.setattr(p, "APPLY_SETTLE_SECONDS", 0)
    monkeypatch.setattr(p, "HOLDER_ATTACH_POLL", 0)


def wita(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=WITA).astimezone(timezone.utc)


class HolderHost(KeepHost):
    """`KeepHost` plus the session holder `whatsapp1-holder` (collector `whatsapp1`), and `whatsapp2-holder` (collector
    `whatsapp2`) when `holders` names "2". Each holder binds its socket directory; the synthetic messaging Dockerfile copies only
    whatsmeow's main.go, so a change to serve.go is a holder-only change. Images carry protocol labels from `ranges`;
    a container started on an image in `broken` exits; `attaching` lists holder containers whose log shows an attach."""

    def __init__(self, tmp_path, monkeypatch, *, at=None, holders=("1",)):
        super().__init__(tmp_path, monkeypatch)
        self.sockets = {}
        for number in holders:
            holder, directory = f"whatsapp{number}-holder", tmp_path / "private" / f"whatsapp{number}-holder"
            directory.mkdir(parents=True)
            self.sockets[holder] = directory
            self.units[holder] = {"image": "old", "environment": {"SYNTHETIC": holder},
                                  "volumes": [{"type": "bind", "source": str(directory), "target": p.HOLDER_SOCKETS}]}
            self.containers[holder] = {"Id": holder + "0", "Image": OLD_IMAGE, "State": {"Running": True}, "Config": {"Labels": {}}}
            self.project["services"][holder] = "whatsapp-holder"
        self.project["sessions"] = {f"whatsapp{number}-holder": {"collector": f"whatsapp{number}"} for number in holders}
        self.write_compose()
        self.files["services/messaging/Dockerfile"] = (
            "100644", self.files["services/messaging/Dockerfile"][1].replace("COPY whatsapp/whatsmeow ./",
                                                                             "COPY whatsapp/whatsmeow/main.go ./"))
        self.files["whatsapp/whatsmeow/Dockerfile"] = ("100644", HOLDER_DOCKERFILE.format(go="b" * 64))
        self.files["whatsapp/whatsmeow/serve.go"] = ("100644", "package main // serve 1\n")
        self.ranges = {"holder": ("1", "1"), "collector": ("1", "1")}
        self.broken, self.silent, self.markers = set(), set(), []
        self.now = at or wita(10, 10)
        monkeypatch.setattr(p, "build_image", self.build)
        monkeypatch.setattr(p, "holder_clock", lambda: self.now)
        monkeypatch.setattr(p, "now", lambda: self.now.strftime("%Y-%m-%dT%H:%M:%SZ"))
        monkeypatch.setattr(p, "holder_log", self.log)
        monkeypatch.setattr(p, "SESSION_OWNER", (os.getuid(), os.getgid()))
        monkeypatch.setattr(p, "serving_generation", lambda settings, http: None)

    def image_of(self, role, sha):
        return "sha256:" + hashlib.sha256(f"{role} {sha}".encode()).hexdigest()

    def build(self, source, sha, target, *, check=False, verified=None):
        role = "holder" if target == "whatsapp-holder" else "collector"
        image = self.image_of(role, sha)
        low, high = self.ranges[role]
        self.images[image] = {"Id": image, "Config": {"Labels": {
            p.REVISION_LABEL: sha, f"io.deedkit.{role}.protocol.min": low, f"io.deedkit.{role}.protocol.max": high}}}
        return image

    def log(self, container, since):
        holder = next(name for name, value in self.containers.items() if value["Id"] == container)
        return [] if holder in self.silent else ['{"event": "holder.claimed"}', '{"event": "holder.attached"}']

    def run(self, argv, **kwargs):
        if argv[:2] == ["docker", "compose"] and "stop" in argv:
            for service in argv[argv.index("stop") + 1:]:
                if service in self.sockets and (self.sockets[service] / p.PLANNED_STOP).exists():
                    self.markers.append((service, whatsapp_holder.planned_stop(self.sockets[service], time.time() - 60),
                                         stat.S_IMODE((self.sockets[service] / p.PLANNED_STOP).stat().st_mode)))
        result = super().run(argv, **kwargs)
        if argv[:2] == ["docker", "compose"] and "stop" in argv:  # as Docker reports a stopped container
            for service in argv[argv.index("stop") + 1:]:
                self.containers[service]["State"] = {"Running": False, "Status": "exited"}
        if argv[:2] == ["docker", "compose"] and "up" in argv:
            for service in argv[argv.index("180") + 1:]:
                self.order.append(("up", service))
                if self.containers[service]["Image"] in self.broken:
                    self.containers[service]["State"] = {"Running": False, "Status": "exited"}
        return result

    def change_holder(self, text):
        self.files["whatsapp/whatsmeow/serve.go"] = ("100644", f"package main // {text}\n")

    def change_collector(self, text):
        self.files["services/messaging/messaging_service/app.py"] = ("100644", f"APP = {text!r}\n")

    def ledger(self):
        return p.holder_ledger(self.state)

    def outcome(self, result, service):
        return result["services"]["synthetic"][service]

    def ups(self):
        return [service for action, service in self.order if action == "up"]


def released(host, sha, at):
    host.now, host.order = at, []
    return host.release(sha)


def first(tmp_path, monkeypatch, holders=("1",)):
    """A host after its first release (everything unlabeled is recreated), in the daytime."""
    host = HolderHost(tmp_path, monkeypatch, holders=holders)
    released(host, SHAS[0], wita(10, 9))
    return host


# spec: ANYM-896
def test_config_accepts_session_marks_only_on_a_holder_target_and_normalizes_the_services(tmp_path):
    compose = tmp_path / "compose.json"
    compose.write_text("{}")
    def written(services, **extra):
        path = tmp_path / "production.json"
        path.write_text(json.dumps({"version": 1, "state_directory": str(tmp_path / "state"), **extra, "projects": [
            {"name": "synthetic", "directory": str(tmp_path), "files": [str(compose)], "services": services}]}))
        path.chmod(0o600)
        return path
    holder = {"target": "whatsapp-holder", "session": True, "collector": "whatsapp1"}
    value = p.config(written({"whatsapp1": "whatsapp-native", "whatsapp1-holder": holder}))
    assert value["projects"][0]["services"] == {"whatsapp1": "whatsapp-native", "whatsapp1-holder": "whatsapp-holder"}
    assert value["projects"][0]["sessions"] == {"whatsapp1-holder": {"collector": "whatsapp1"}}
    assert p.holder_window(value)[1:] == (180, 300)
    plain = p.config(written({"whatsapp1": "whatsapp-native"}))
    assert "sessions" not in plain["projects"][0] and p.settings_digest(plain) == p.settings_digest(
        {**plain, "session_window": "02:00-04:00"})
    refusals = {
        "session_target_unsupported": {"telegram": {"target": "runtime", "session": True, "collector": "whatsapp1"},
                                       "whatsapp1": "whatsapp-native"},
        "session_mark_invalid": {"whatsapp1": "whatsapp-native", "whatsapp1-holder": {**holder, "window": "now"}},
    }
    for code, services in refusals.items():
        with pytest.raises(p.Refused, match=code):
            p.config(written(services))
    for services in ({"whatsapp1-holder": {**holder, "collector": "absent"}}, {"whatsapp1-holder": {**holder, "session": 1},
                                                                             "whatsapp1": "whatsapp-native"},
                     {"whatsapp1-holder": {"target": "whatsapp-holder", "session": True}}):
        with pytest.raises(p.Refused, match="session_mark_invalid"):
            p.config(written(services))
    with pytest.raises(p.Refused, match="session_window_invalid"):
        p.config(written({"whatsapp1": "whatsapp-native"}, session_window="05:00-03:00"))
    with pytest.raises(p.Refused, match="session_timezone_invalid"):
        p.config(written({"whatsapp1": "whatsapp-native"}, session_timezone="Nowhere/Synthetic"))


# spec: ANYM-896
def test_the_window_is_03_to_05_bali_time_and_the_pass_start_varies_by_date_inside_03_30_to_04_30():
    settings = {}
    begin, end = p.holder_window_at(settings, wita(10, 10))
    assert (begin, end) == (wita(11, 3), wita(11, 5))
    assert p.holder_window_at(settings, wita(11, 4)) == (wita(11, 3), wita(11, 5))
    assert p.holder_next_window(settings, wita(11, 4)) == (wita(12, 3), wita(12, 5))
    assert p.holder_window_text(settings, begin, end) == "2026-11-10T19:00Z-21:00Z UTC, 2026-11-11 03:00-05:00 WITA"
    starts = []
    for offset in range(60):
        begin = wita(1, 3) + timedelta(days=offset)
        start = p.holder_pass_time(settings, begin, begin + timedelta(hours=2))
        assert start == p.holder_pass_time(settings, begin, begin + timedelta(hours=2))  # deterministic
        assert begin + timedelta(minutes=30) <= start < begin + timedelta(minutes=90)
        starts.append((start - begin).total_seconds())
    assert len(set(starts)) > 30


# spec: ANYM-896
def test_a_holder_change_in_the_daytime_is_deferred_with_its_next_window_and_the_rest_goes(tmp_path, monkeypatch):
    host = first(tmp_path, monkeypatch)
    before = host.ids()
    host.change_holder("serve 2")
    result = released(host, SHAS[1], wita(10, 10))
    assert host.stopped() == [] and host.ids() == before
    deferred = host.outcome(result, HOLDER)["deferred"]
    assert deferred["parts"] == ["code"] and deferred["revision"] == SHAS[0] and deferred["container"] == before[HOLDER]
    assert deferred["line"] == "deferred (code, next window 2026-11-10T19:00Z-21:00Z UTC, 2026-11-11 03:00-05:00 WITA)"
    [project] = result["rollback"]["projects"]
    assert project["kept"][HOLDER] == SHAS[0]
    assert project["inputs"][HOLDER]["digest"] == host.containers[HOLDER]["Config"]["Labels"][p.INPUT_LABEL]
    pending = host.ledger()["pending"]["synthetic/" + HOLDER]
    assert pending["parts"] == ["code"] and pending["release"] == SHAS[1]
    # A later daytime release that changes nothing else still defers it, from the first deferral on.
    later = released(host, SHAS[2], wita(10, 15))
    assert "deferred" in host.outcome(later, HOLDER) and host.stopped() == []
    assert host.ledger()["pending"]["synthetic/" + HOLDER]["since"] == "2026-11-10T02:00:00Z"


# spec: ANYM-896
def test_inside_the_window_a_holder_change_goes_once_and_a_second_change_that_night_waits_for_the_next(
        tmp_path, monkeypatch):
    host = first(tmp_path, monkeypatch)
    host.change_holder("serve 2")
    result = released(host, SHAS[1], wita(11, 3, 40))
    assert host.stopped() == [HOLDER] and host.outcome(result, HOLDER) == {"recreated": ["code"]}
    [event] = [event for event in host.ledger()["events"] if event["kind"] == "planned" and HOLDER in event["service"]]
    assert event == {"service": "synthetic/" + HOLDER, "kind": "planned", "window": "2026-11-11", "release": SHAS[1],
                     "at": "2026-11-10T19:40:00Z"}
    host.change_holder("serve 3")
    second = released(host, SHAS[2], wita(11, 4, 10))
    assert host.stopped() == []
    assert host.outcome(second, HOLDER)["deferred"]["next_window"].endswith("2026-11-12 03:00-05:00 WITA")


# spec: ANYM-896
def test_a_stopped_holder_is_recreated_at_once_in_the_daytime_and_names_its_reason(tmp_path, monkeypatch):
    host = first(tmp_path, monkeypatch)
    host.containers[HOLDER]["State"] = {"Running": False, "Status": "exited"}
    host.change_holder("serve 2")
    result = released(host, SHAS[1], wita(10, 14))
    assert host.stopped() == [HOLDER] and "recreated" in host.outcome(result, HOLDER)
    assert host.containers[HOLDER]["State"]["Running"]
    [event] = [event for event in host.ledger()["events"] if event["release"] == SHAS[1]]
    assert (event["kind"], event["reason"]) == ("recovery", "stopped")


# spec: ANYM-896
def test_an_expand_migration_on_collector_leases_recreates_the_collectors_and_defers_the_holders(tmp_path, monkeypatch):
    host = first(tmp_path, monkeypatch)
    host.files["services/messaging/messaging_service/migrations/084_collector_leases.sql"] = (
        "100644", "-- expand: synthetic collector_leases column\n")
    result = released(host, SHAS[1], wita(10, 11))
    # A holder copies no migration: the migration alone changes no holder.
    assert {host.outcome(result, holder).keys() == {"kept"} for holder in host.sockets} == {True}
    assert sorted(host.stopped()) == ["telegram", "whatsapp1", "whatsapp2"]
    assert p.read(host.state / SHAS[1] / "result.json")["services"]["synthetic"][COLLECTOR] == {"recreated": ["code", "migration"]}
    host.files["services/messaging/messaging_service/migrations/085_collector_leases.sql"] = ("100644", "-- next\n")
    host.change_holder("serve 2")
    both = released(host, SHAS[2], wita(10, 12))
    assert sorted(host.stopped()) == ["telegram", "whatsapp1", "whatsapp2"]
    assert all("deferred" in host.outcome(both, holder) for holder in host.sockets)
    assert host.outcome(both, COLLECTOR) == {"recreated": ["code", "migration"]}


# spec: ANYM-896
def test_a_compatible_collector_only_change_keeps_the_holder_and_an_incompatible_one_stops_nothing(tmp_path, monkeypatch):
    host = first(tmp_path, monkeypatch)
    holder = host.ids()[HOLDER]
    host.change_collector("2")
    result = released(host, SHAS[1], wita(11, 3, 40))
    assert COLLECTOR in host.stopped() and HOLDER not in host.stopped() and host.ids()[HOLDER] == holder
    assert "kept" in host.outcome(result, HOLDER)
    host.change_collector("3")
    host.ranges["collector"] = ("2", "2")  # the candidate collector no longer speaks the running holder's protocol 1
    host.now, host.order = wita(11, 4), []
    host.main = SHAS[2]
    p.prepare(host.settings, SHAS[2])
    with pytest.raises(p.Refused, match="session_pair_incompatible"):
        p.apply(host.settings, SHAS[2])
    assert host.order == [] and not (host.state / SHAS[2] / "attempted.json").exists()


# spec: ANYM-896
def test_a_holder_and_collector_change_goes_as_a_pair_collector_stopped_first_and_started_after_the_holder(
        tmp_path, monkeypatch):
    host = first(tmp_path, monkeypatch)
    host.change_holder("serve 2")
    host.files["whatsapp/whatsmeow/main.go"] = ("100644", "package main // both\n")
    released(host, SHAS[1], wita(11, 3, 40))
    pair = [entry for entry in host.order if entry[1] in (HOLDER, COLLECTOR)]
    assert pair == [("stop", COLLECTOR), ("stop", HOLDER), ("up", HOLDER), ("up", COLLECTOR)]
    # In the daytime the collector goes alone (it speaks to the running holder) and the holder waits.
    host.change_holder("serve 3")
    host.files["whatsapp/whatsmeow/main.go"] = ("100644", "package main // both again\n")
    result = released(host, SHAS[2], wita(11, 9))
    assert COLLECTOR in host.stopped() and HOLDER not in host.stopped() and "deferred" in host.outcome(result, HOLDER)


# spec: ANYM-896
@pytest.mark.parametrize("failure", ["holder check", "collector attach"])
def test_a_pair_failing_its_holder_check_or_collector_attach_is_unhealthy_and_its_rollback_restores_both_holder_first(
        tmp_path, monkeypatch, failure):
    host = first(tmp_path, monkeypatch)
    previous = {service: host.containers[service]["Image"] for service in (HOLDER, COLLECTOR)}
    host.change_holder("serve 2")
    host.files["whatsapp/whatsmeow/main.go"] = ("100644", "package main // both\n")
    if failure == "holder check":
        host.broken.add(host.image_of("holder", SHAS[1]))
    else:
        host.silent.add(HOLDER)
    with pytest.raises(p.Refused, match="rollback --sha"):
        released(host, SHAS[1], wita(11, 3, 40))
    assert p.read(host.state / SHAS[1] / "result.json")["status"] == "unhealthy"
    if failure == "holder check":  # nothing attached to the failed holder
        assert ("up", COLLECTOR) not in host.order
    host.silent.clear()
    host.order = []
    p.rollback(host.settings, SHAS[1])
    assert {service: host.containers[service]["Image"] for service in (HOLDER, COLLECTOR)} == previous
    ups = host.ups()
    assert ups.index(HOLDER) < ups.index(COLLECTOR)


def deferred_host(tmp_path, monkeypatch):
    host = first(tmp_path, monkeypatch)
    host.change_holder("serve 2")
    released(host, SHAS[1], wita(10, 10))
    return host


# spec: ANYM-896
def test_the_night_pass_recreates_a_deferred_holder_with_the_planned_stop_marker_and_records_it(tmp_path, monkeypatch):
    host = deferred_host(tmp_path, monkeypatch)
    old = host.ids()
    host.now, host.order = wita(11, 3, 45), []
    result = p.holder_pass(host.settings, None)
    assert result["status"] == "applied" and result["outcomes"] == {
        "synthetic/" + HOLDER: {"status": "recreated", "services": [HOLDER]}}
    assert result["counts"] == {"planned": 1, "recovery": 0, "flagged": 0, "rollback": 0}
    assert host.stopped() == [HOLDER] and host.containers[HOLDER]["Image"] == host.image_of("holder", SHAS[1])
    assert host.ids()[COLLECTOR] == old[COLLECTOR]
    # The collector read the marker as the window's planned stop; it is gone after the holder is back.
    assert host.markers == [(HOLDER, ("session_window", wita(11, 5).isoformat()), 0o600)]
    assert not (host.sockets[HOLDER] / p.PLANNED_STOP).exists()
    assert host.ledger()["pending"] == {}
    current = p.read(host.state / "current.json")
    assert current == p.read(host.state / SHAS[1] / "result.json")
    [project] = current["rollback"]["projects"]
    assert HOLDER not in project["kept"] and project["running"][HOLDER]["container"] == host.ids()[HOLDER]
    # The next release finds the holder up to date; without a marker the collector would report holder_restart.
    host.order = []
    after = released(host, SHAS[2], wita(11, 9))
    assert host.stopped() == [] and host.outcome(after, HOLDER)["kept"]["revision"] == SHAS[1]
    assert whatsapp_holder.planned_stop(host.sockets[HOLDER], 0) == ("holder_restart", None)


# spec: ANYM-896
def test_a_change_undone_by_a_daytime_rollback_is_recorded_as_nothing_pending(tmp_path, monkeypatch):
    host = deferred_host(tmp_path, monkeypatch)
    host.change_holder("serve 1")  # the next release reverts it
    released(host, SHAS[2], wita(10, 16))
    assert host.stopped() == [] and "synthetic/" + HOLDER in host.ledger()["pending"]
    host.now, host.order = wita(11, 3, 45), []
    assert p.holder_pass(host.settings, None)["status"] == "nothing pending"
    assert host.order == [] and host.ledger()["pending"] == {}
    assert host.ledger()["passes"]["2026-11-11"] == {"status": "nothing pending"}


# spec: ANYM-896
def test_a_holder_failing_its_check_at_03_40_is_rolled_back_inside_the_pass(tmp_path, monkeypatch):
    host = deferred_host(tmp_path, monkeypatch)
    previous = host.containers[HOLDER]["Image"]
    host.broken.add(host.image_of("holder", SHAS[1]))
    host.now, host.order = wita(11, 3, 40), []
    result = p.holder_pass(host.settings, None)
    assert result["status"] == "rolled_back" and "not running steadily" in result["outcomes"]["synthetic/" + HOLDER]["check"]
    assert host.containers[HOLDER]["Image"] == previous and host.containers[HOLDER]["State"]["Running"]
    assert result["counts"] == {"planned": 1, "recovery": 0, "flagged": 0, "rollback": 1}
    assert p.read(host.state / SHAS[1] / ("session-" + "20261110T194000Z") / "result.json") == result
    [project] = p.read(host.state / "current.json")["rollback"]["projects"]
    assert project["running"][HOLDER]["container"] == host.ids()[HOLDER] and project["kept"][HOLDER] == SHAS[0]
    assert "synthetic/" + HOLDER in host.ledger()["pending"] and p.unfinished(host.state) == []
    # The restored container carries the label its receipt names: a later daytime release keeps it deferred.
    host.order = []
    later = released(host, SHAS[2], wita(11, 10))
    assert host.stopped() == [] and "deferred" in host.outcome(later, HOLDER)


# spec: ANYM-896
def test_a_pair_failing_in_the_pass_restores_both_holder_first(tmp_path, monkeypatch):
    host = deferred_host(tmp_path, monkeypatch)
    # The collector differs from the release too (recreated by hand meanwhile): the pass recreates the pair.
    host.containers[COLLECTOR] = {**host.containers[COLLECTOR], "Id": "whatsapp1-by-hand"}
    previous = {service: host.containers[service]["Image"] for service in (HOLDER, COLLECTOR)}
    host.silent.add(HOLDER)
    host.now, host.order = wita(11, 4), []
    result = p.holder_pass(host.settings, None)
    assert result["outcomes"]["synthetic/" + HOLDER]["services"] == [HOLDER, COLLECTOR]
    assert result["status"] == "rolled_back" and result["counts"]["rollback"] == 2
    assert host.order[:2] == [("stop", COLLECTOR), ("stop", HOLDER)]
    assert host.ups() == [HOLDER, COLLECTOR, HOLDER, COLLECTOR]
    assert {service: host.containers[service]["Image"] for service in (HOLDER, COLLECTOR)} == previous


def nightly(host, http=None):
    http = http or {"synthetic": 1}
    halts = []
    def halt(reason, status="halted", *, code, **fields):
        halts.append(code)
        return {"status": status, "code": code, "reason": reason}
    current = p.read(host.state / "current.json")
    outcome = p.holder_nightly(host.settings, http, host.state, current, {"config_digest": p.digest(http)},
                               {"status": "current"}, auto=True, dry_run=False, halt=halt)
    return outcome, halts


# spec: ANYM-896
def test_autodeploy_runs_the_pass_once_from_the_nights_start_and_a_crash_after_started_refuses_a_second(
        tmp_path, monkeypatch):
    host = deferred_host(tmp_path, monkeypatch)
    begin = wita(11, 3)
    start = p.holder_pass_time(host.settings, begin, begin + timedelta(hours=2))
    host.now = start - timedelta(seconds=1)
    assert nightly(host) == ({"status": "current"}, []) and host.order == []
    host.now = start
    def crash(argv, **kwargs):
        if argv[:2] == ["docker", "compose"] and "stop" in argv:
            raise KeyboardInterrupt  # the process dies after `started`, before any stop
        return HolderHost.run(host, argv, **kwargs)
    monkeypatch.setattr(p, "run", crash)
    with pytest.raises(KeyboardInterrupt):
        nightly(host)
    [unfinished] = p.unfinished(host.state)
    assert unfinished.startswith(SHAS[1] + "/session-")
    assert p.read(host.state / unfinished / "attempted.json")["status"] == "started"
    monkeypatch.setattr(p, "run", host.run)
    host.now, host.order = start + timedelta(minutes=5), []
    assert nightly(host) == ({"status": "current"}, []) and host.order == []
    assert "unfinished" in p.unresolved(host.state, tmp_path / "http-state")
    assert p.holder_flagged(host.settings, {"state_directory": str(tmp_path / "http-state")},
                            (HOLDER, "incident:synthetic-record"))["status"] == "refused"


# spec: ANYM-896
def test_a_call_wait_running_past_05_00_stops_nothing_and_defers(tmp_path, monkeypatch):
    host = deferred_host(tmp_path, monkeypatch)
    bounds = []
    def calls(settings, deadline):
        bounds.append(deadline - time.monotonic())
        raise p.CallActive("a call is active: synthetic")
    monkeypatch.setattr(p, "wait_for_calls", calls)
    host.now, host.order = wita(11, 4, 50), []
    result = p.holder_pass(host.settings, None)
    assert result["status"] == "deferred" and result["reason"] == "deferred (window closed during call wait)"
    assert host.order == [] and 590 < bounds[0] <= 600  # bounded by the window's end, 10 minutes away
    assert result["counts"] == {"planned": 0, "recovery": 0, "flagged": 0, "rollback": 0}
    assert "synthetic/" + HOLDER in host.ledger()["pending"] and p.unfinished(host.state) == []


# spec: ANYM-896
def test_the_urgent_flag_recreates_two_named_holders_at_once_in_the_daytime_with_its_reference(tmp_path, monkeypatch):
    host = first(tmp_path, monkeypatch, ("1", "2"))
    names, reference = p.holder_flag(host.settings, "whatsapp1-holder,synthetic/whatsapp2-holder",
                                     "incident:docs/operations/incident-recovery.md record 2026-11-10")
    assert names == ["synthetic/whatsapp1-holder", "synthetic/whatsapp2-holder"]
    before = host.ids()
    host.now, host.order = wita(10, 13), []
    result = p.holder_pass(host.settings, None, flagged=set(names), reference=reference)
    assert sorted(host.stopped()) == ["whatsapp1-holder", "whatsapp2-holder"]
    assert all(host.ids()[holder] != before[holder] for holder in host.sockets)
    assert result["counts"] == {"planned": 0, "recovery": 0, "flagged": 2, "rollback": 0}
    flagged = [event for event in host.ledger()["events"] if event["kind"] == "flagged"]
    assert {event["reference"] for event in flagged} == {reference} and len(flagged) == 2
    assert host.ledger()["passes"] == {}  # a flagged pass is not the night's pass


# spec: ANYM-896
@pytest.mark.parametrize("services, reason", [
    ("whatsapp1-holder", "urgent:it is broken"), ("whatsapp1-holder", "incident:"), ("whatsapp1-holder", "owner: "),
    ("whatsapp1-holder", None), ("whatsapp1", "incident:record"), ("absent", "incident:record"), ("", "incident:record"),
    ("whatsapp1-holder,whatsapp1-holder", "incident:record")])
def test_each_invalid_urgent_flag_is_refused_before_any_stop(tmp_path, monkeypatch, services, reason):
    host = first(tmp_path, monkeypatch)
    host.order = []
    with pytest.raises(p.Refused, match="session_flag_invalid"):
        p.deploy(host.settings, {}, flag=(services, reason))
    assert host.order == []


# spec: ANYM-896
def test_status_lists_each_pending_change_with_its_next_window_and_a_stale_one_after_two_windows(tmp_path, monkeypatch):
    host = deferred_host(tmp_path, monkeypatch)
    [line] = p.holder_pending(host.settings)
    assert line == {"service": "synthetic/" + HOLDER, "parts": ["code"], "release": SHAS[1], "since": "2026-11-10T02:00:00Z",
                    "next_window": "2026-11-10T19:00Z-21:00Z UTC, 2026-11-11 03:00-05:00 WITA"}
    assert "code" not in p.holder_pending(host.settings, wita(12, 4))[0]  # one window passed
    assert p.holder_pending(host.settings, wita(12, 6))[0]["code"] == "session_deferral_stale"
    monkeypatch.setattr(p, "config", lambda path: host.settings)
    monkeypatch.setattr(p, "project_services", lambda settings, current: [])
    monkeypatch.setattr(p, "disk_warnings", lambda: [])
    monkeypatch.setattr(p, "PAUSE", tmp_path / "absent.pause")
    args = p.argument_parser().parse_args(["status", "--config", str(tmp_path / "production.json"),
                                           "--http-config", str(tmp_path / "absent.json")])
    assert p.locked_action(args)["session_pending"] == [line]


# spec: ANYM-896
def test_rollback_of_a_pair_release_restores_both_holder_first(tmp_path, monkeypatch):
    host = first(tmp_path, monkeypatch)
    previous = {service: host.containers[service]["Image"] for service in (HOLDER, COLLECTOR)}
    host.change_holder("serve 2")
    host.files["whatsapp/whatsmeow/main.go"] = ("100644", "package main // both\n")
    released(host, SHAS[1], wita(11, 3, 40))
    host.order = []
    p.rollback(host.settings, SHAS[1])
    assert {service: host.containers[service]["Image"] for service in (HOLDER, COLLECTOR)} == previous
    assert host.order.index(("stop", COLLECTOR)) < host.order.index(("stop", HOLDER))
    assert host.ups().index(HOLDER) < host.ups().index(COLLECTOR)
