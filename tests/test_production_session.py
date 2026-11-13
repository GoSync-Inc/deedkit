"""ANYM-896: session holders restart at most once a night, inside the window; a failed night recreate rolls back."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import time

import pytest

from scripts import production as p
from test_production import OLD_IMAGE, Clock, KeepHost

from messaging_service import whatsapp_holder

WITA = timezone(timedelta(hours=8))
HOLDER_LOG = p.holder_log
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
    # Keep the real attach interval and deadline, advancing only this synthetic host's time. Setting the poll
    # interval to zero left three negative cases spinning on the real monotonic clock for 120 seconds each.
    clock = Clock()
    clock.now = time.monotonic()
    monkeypatch.setattr(p, "time", clock)


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
        if argv[:2] == ["docker", "ps"] and next(word.partition("service=")[2] for word in argv
                                                 if word.startswith("label=com.docker.compose.service=")) not in self.containers:
            self.calls.append(argv)
            return ""  # a service that never had a container
        if argv[:2] == ["docker", "compose"] and "--no-start" in argv:  # create-holder: created, never started
            self.calls.append(argv)
            override = json.loads(Path(argv[len(argv) - 1 - argv[::-1].index("-f") + 1]).read_text())
            service = argv[-1]
            self.containers[service] = {"Id": service + "created", "Image": override["services"][service]["image"],
                                        "State": {"Running": False, "Status": "created"}, "Config": {"Labels": {}}}
            return ""
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
def test_attach_polling_keeps_the_full_deadline_on_the_synthetic_hosts_clock(tmp_path, monkeypatch):
    host = first(tmp_path, monkeypatch)
    assert p.time is not time, "the synthetic host must advance its own clock, without waiting in real time"
    clock = p.time
    clock.sleeps.clear()
    start = clock.monotonic()
    monkeypatch.setattr(p, "run", lambda *args, **kwargs: "synthetic-holder")
    monkeypatch.setattr(p, "holder_log", lambda *args: [])
    assert "within 120 s" in p.holder_attached([], host.settings["projects"][0], [HOLDER], "synthetic")
    assert clock.monotonic() - start == p.HOLDER_ATTACH_S == 120
    assert clock.sleeps == [p.HOLDER_ATTACH_POLL] * 24
    # An attach on the last poll before the bound still passes; the failure path cannot replace the polling loop.
    start = clock.monotonic()
    monkeypatch.setattr(p, "holder_log", lambda *args: ['{"event":"holder.attached"}']
                        if clock.monotonic() - start >= 115 else [])
    assert p.holder_attached([], host.settings["projects"][0], [HOLDER], "synthetic") is None
    assert clock.monotonic() - start == 115


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


def brand_new_holder(tmp_path, monkeypatch):
    """Cutover step 3 done on a host where whatsapp1-holder never ran: its mark, services entry and Compose unit, no
    container. Autodeploy is paused (step 0) by the caller."""
    host = HolderHost(tmp_path, monkeypatch)
    del host.containers[HOLDER]
    monkeypatch.setattr(p, "PAUSE", tmp_path / "autodeploy.pause")
    return host


# spec: ANYM-937
def test_a_brand_new_holder_is_created_unstarted_so_prepare_reads_it_and_the_release_starts_it_as_a_pair(
        tmp_path, monkeypatch):
    host = brand_new_holder(tmp_path, monkeypatch)
    host.main = SHAS[0]
    with pytest.raises(p.Refused, match="exactly one running container"):  # the defect: no container, no prepare
        p.prepare(host.settings, SHAS[0])
    p.PAUSE.write_text("holder cutover\n")
    host.main = SHAS[1]
    created = p.create_holder(host.settings, HOLDER, SHAS[1])
    image = host.image_of("holder", SHAS[1])
    assert (created["status"], created["container"], created["image"]) == ("created", HOLDER + "created", image)
    assert created["undo"].startswith("docker compose") and created["undo"].endswith(f"rm --stop {HOLDER}")
    # Nothing started or stopped: no provider session begins before apply's pair step.
    assert host.containers[HOLDER]["State"] == {"Running": False, "Status": "created"} and host.order == []
    assert not [path for path in host.state.iterdir() if path.name.startswith(".create-holder-")]
    result = released(host, SHAS[1], wita(10, 14))
    pair = [entry for entry in host.order if entry[1] in (HOLDER, COLLECTOR)]
    assert pair == [("stop", COLLECTOR), ("stop", HOLDER), ("up", HOLDER), ("up", COLLECTOR)]
    assert host.containers[HOLDER]["State"]["Running"] and host.containers[HOLDER]["Image"] == image
    assert "recreated" in host.outcome(result, HOLDER)
    [event] = [event for event in host.ledger()["events"] if event["service"] == "synthetic/" + HOLDER]
    assert (event["kind"], event["reason"]) == ("recovery", "created")


class FirstCutoverHost(HolderHost):
    """Cutover steps 0-3 on a host whose whatsapp1 runs in child mode: the configuration and Compose files name
    whatsapp1-holder, which binds the session directory and the lock directory, and the running whatsapp1 container
    still mounts that session directory; the holder has no container yet. Docker answers the session-opener scan with
    every container's Compose labels and mounts (as SessionHost in test_production does); `others` are foreign ones."""

    def __init__(self, tmp_path, monkeypatch):
        super().__init__(tmp_path, monkeypatch)
        self.session, self.others = tmp_path / "private" / "whatsapp1-session", []
        self.session.mkdir(parents=True)
        self.units[HOLDER]["volumes"] += [
            {"type": "bind", "source": str(self.session), "target": "/sessions"},
            {"type": "bind", "source": str(tmp_path / "private" / "session-locks"), "target": p.SESSION_LOCKS}]
        self.units[HOLDER]["stop_grace_period"] = "25s"
        self.write_compose()
        del self.containers[HOLDER]
        self.containers[COLLECTOR]["Mounts"] = [{"Type": "bind", "Source": str(self.session), "Destination": "/sessions"}]
        monkeypatch.setattr(p, "PAUSE", tmp_path / "autodeploy.pause")
        p.PAUSE.write_text("holder cutover\n")

    def run(self, argv, **kwargs):
        for service, container in self.containers.items():
            container.setdefault("Config", {}).setdefault("Labels", {}).update({
                "com.docker.compose.project": "synthetic", "com.docker.compose.service": service,
                "com.docker.compose.oneoff": "False"})
            container.setdefault("Mounts", [{"Type": "bind", "Source": volume["source"], "Destination": volume["target"]}
                                            for volume in self.units[service].get("volumes", [])])
        everything = [*self.containers.values(), *self.others]
        if argv == ["docker", "ps", "--all", "--quiet", "--no-trunc"]:
            self.calls.append(argv)
            return "".join(container["Id"] + "\n" for container in everything)
        if argv[:2] == ["docker", "inspect"] and "--format" not in argv and (len(argv) > 3 or argv[2].startswith("other")):
            self.calls.append(argv)
            return json.dumps([next(item for item in everything if item["Id"] == identity) for identity in argv[2:]])
        if argv[:2] == ["docker", "start"]:
            self.calls.append(argv)
            service = next(name for name, container in self.containers.items() if container["Id"] == argv[2])
            self.order.append(("start", service))
            self.containers[service]["State"] = {"Running": True}
            return ""
        return super().run(argv, **kwargs)


# spec: ANYM-937
def test_a_first_cutover_admits_the_child_mode_collector_it_stops_first_and_still_refuses_a_foreign_opener(
        tmp_path, monkeypatch):
    host = FirstCutoverHost(tmp_path, monkeypatch)
    host.main = SHAS[1]
    p.create_holder(host.settings, HOLDER, SHAS[1])
    host.others.append({"Id": "other1", "Name": "/other1", "Image": OLD_IMAGE, "State": {"Running": True},
                        "Config": {"Labels": {"com.docker.compose.project": "elsewhere"}},
                        "Mounts": [{"Type": "bind", "Source": str(host.session), "Destination": "/sessions"}]})
    p.prepare(host.settings, SHAS[1])
    with pytest.raises(p.Refused, match="foreign_session_opener: container other1"):
        p.apply(host.settings, SHAS[1])
    assert host.order == []
    # Without the foreign container the child-mode collector, which this apply stops before the holder, is admitted.
    host.others.clear()
    host.main, host.order = SHAS[2], []
    released(host, SHAS[2], wita(10, 14))
    pair = [entry for entry in host.order if entry[1] in (HOLDER, COLLECTOR)]
    assert pair == [("stop", COLLECTOR), ("stop", HOLDER), ("up", HOLDER), ("up", COLLECTOR)]
    assert all(host.containers[service]["State"]["Running"] for service in (HOLDER, COLLECTOR))


# spec: ANYM-937
def test_a_first_holder_failing_at_apply_stays_stopped_and_its_collector_restarts_its_own_child_mode_container(
        tmp_path, monkeypatch):
    host = FirstCutoverHost(tmp_path, monkeypatch)
    child = {key: host.containers[COLLECTOR][key] for key in ("Id", "Image")}
    host.main = SHAS[1]
    p.create_holder(host.settings, HOLDER, SHAS[1])
    host.broken.add(host.image_of("holder", SHAS[1]))  # e.g. a wrong holder DSN file: the holder exits at once
    with pytest.raises(p.Refused, match="rollback --sha"):
        released(host, SHAS[1], wita(10, 14))
    # The holder is never started again; the collector runs its very container from before the release.
    assert {key: host.containers[COLLECTOR][key] for key in ("Id", "Image")} == child
    assert host.containers[COLLECTOR]["State"]["Running"] and not host.containers[HOLDER]["State"].get("Running")
    assert host.ups().count(HOLDER) == 1 and COLLECTOR not in host.ups() and ("start", COLLECTOR) in host.order
    result = p.read(host.state / SHAS[1] / "result.json")
    assert result["status"] == "unhealthy" and set(result["rollback"]["projects"][0]["kept"]) >= {HOLDER, COLLECTOR}
    # The release's rollback leaves that pair as it is: the holder stopped, the collector on its own container.
    host.order = []
    p.rollback(host.settings, SHAS[1])
    assert not [entry for entry in host.order if entry[1] in (HOLDER, COLLECTOR)]
    assert host.containers[COLLECTOR]["Id"] == child["Id"] and host.containers[COLLECTOR]["State"]["Running"]
    assert not host.containers[HOLDER]["State"].get("Running")


# spec: ANYM-937
@pytest.mark.parametrize("case", ["replaced", "settings changed"])
def test_a_first_cutover_that_cannot_return_to_its_child_mode_container_starts_nothing_and_names_the_hand_undo(
        tmp_path, monkeypatch, case):
    host = FirstCutoverHost(tmp_path, monkeypatch)
    child = host.containers[COLLECTOR]
    host.main = SHAS[1]
    p.create_holder(host.settings, HOLDER, SHAS[1])
    if case == "replaced":  # the attach fails after Compose replaced the collector: its old container is gone
        host.silent.add(HOLDER)
        monkeypatch.setattr(p, "HOLDER_ATTACH_S", 0)
    else:  # the holder fails, and the old container no longer has the settings prepare recorded
        host.broken.add(host.image_of("holder", SHAS[1]))
        stop = host.run
        def changed(argv, **kwargs):
            result = stop(argv, **kwargs)
            if argv[:2] == ["docker", "compose"] and "stop" in argv and COLLECTOR in argv:
                child["HostConfig"] = {"Memory": 1}
            return result
        monkeypatch.setattr(p, "run", changed)
    with pytest.raises(p.Refused, match="first_cutover_unrestored: .*docs/whatsapp-native.md") as refused:
        released(host, SHAS[1], wita(10, 14))
    assert ("gone" if case == "replaced" else "recorded image and settings") in str(refused.value)
    assert ("start", COLLECTOR) not in host.order
    assert (host.state / SHAS[1] / "attempted.json").exists() and not (host.state / SHAS[1] / "result.json").exists()
    if case == "replaced":  # nothing more was stopped or started: both stay as the failed attach left them
        assert host.order[-2:] == [("up", HOLDER), ("up", COLLECTOR)]
        assert all(host.containers[service]["State"]["Running"] for service in (HOLDER, COLLECTOR))


# spec: ANYM-937
def test_create_holder_creates_nothing_unless_paused_marked_defined_and_absent_and_only_holders_may_be_created(
        tmp_path, monkeypatch):
    host = brand_new_holder(tmp_path, monkeypatch)
    host.main = SHAS[1]
    args = p.argument_parser().parse_args(["create-holder", "--service", HOLDER, "--sha", SHAS[1]])
    assert (args.action, args.service) == ("create-holder", HOLDER)
    with pytest.raises(p.Refused, match="autodeploy pause"):
        p.create_holder(host.settings, HOLDER, SHAS[1])
    p.PAUSE.write_text("holder cutover\n")
    with pytest.raises(p.Refused, match="not a session holder"):
        p.create_holder(host.settings, COLLECTOR, SHAS[1])
    unit = host.units.pop(HOLDER)
    host.write_compose()
    with pytest.raises(p.Refused, match="define no"):
        p.create_holder(host.settings, HOLDER, SHAS[1])
    host.units[HOLDER] = unit
    host.write_compose()
    host.containers[HOLDER] = {"Id": HOLDER + "0", "Image": OLD_IMAGE, "State": {"Running": False, "Status": "exited"},
                               "Config": {"Labels": {}}}
    with pytest.raises(p.Refused, match="already has a container"):
        p.create_holder(host.settings, HOLDER, SHAS[1])
    assert not [call for call in host.calls if call[:2] == ["docker", "compose"] and ("up" in call or "stop" in call)]
    assert not [call for call in host.calls if call[0] == "git"]
    # Only a session service's container may be one that never started; any other selected service is refused as before.
    host.containers["gateway"]["State"] = {"Running": False, "Status": "created"}
    for loose in (False, True):
        with pytest.raises(p.Refused, match="exactly one running container|neither running, restarting nor exited"):
            p.snapshot(host.project, stopped=loose, exited={HOLDER})


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
@pytest.mark.parametrize("failure", ["holder check", "collector attach", "log read timeout"])
def test_a_pair_failing_in_apply_is_restored_holder_first_before_apply_ends_and_recorded_unhealthy(
        tmp_path, monkeypatch, failure):
    host = first(tmp_path, monkeypatch)
    previous = {service: host.containers[service]["Image"] for service in (HOLDER, COLLECTOR)}
    host.change_holder("serve 2")
    host.files["whatsapp/whatsmeow/main.go"] = ("100644", "package main // both\n")
    if failure == "holder check":
        host.broken.add(host.image_of("holder", SHAS[1]))
    elif failure == "collector attach":
        host.silent.add(HOLDER)
    else:
        def timed_out(container, since):
            raise subprocess.TimeoutExpired(["docker", "logs"], 30)
        monkeypatch.setattr(p, "holder_log", timed_out)
    with pytest.raises(p.Refused, match="rollback --sha"):
        released(host, SHAS[1], wita(11, 3, 40))
    # The by-hand apply itself restored the pair, holder first; nothing attached to a failed holder.
    assert {service: host.containers[service]["Image"] for service in (HOLDER, COLLECTOR)} == previous
    assert all(host.containers[service]["State"]["Running"] for service in (HOLDER, COLLECTOR))
    ups = host.ups()
    assert ups[-2:] == [HOLDER, COLLECTOR] and ups.count(COLLECTOR) == (1 if failure == "holder check" else 2)
    result = p.read(host.state / SHAS[1] / "result.json")
    assert result["status"] == "unhealthy" and set(result["rollback"]["projects"][0]["kept"]) >= {HOLDER, COLLECTOR}
    # The release's rollback leaves the restored pair as it is.
    host.order = []
    p.rollback(host.settings, SHAS[1])
    assert HOLDER not in host.stopped() and COLLECTOR not in host.stopped()
    assert {service: host.containers[service]["Image"] for service in (HOLDER, COLLECTOR)} == previous


# spec: ANYM-896
@pytest.mark.parametrize("step", ["collector stop", "holder up"])
def test_a_failed_step_after_a_paired_collector_stopped_restores_the_pair_before_apply_ends(tmp_path, monkeypatch, step):
    host = first(tmp_path, monkeypatch)
    previous = {service: host.containers[service]["Image"] for service in (HOLDER, COLLECTOR)}
    host.change_holder("serve 2")
    host.files["whatsapp/whatsmeow/main.go"] = ("100644", "package main // both\n")
    host.now, host.order, host.main = wita(11, 3, 40), [], SHAS[1]
    p.prepare(host.settings, SHAS[1])
    real, failed = host.run, []
    def flaky(argv, **kwargs):
        release_file = str(host.state / SHAS[1] / "synthetic.json")
        if not failed and release_file in argv and (
                step == "collector stop" and argv[-2:] == ["stop", COLLECTOR] or step == "holder up" and "up" in argv):
            failed.append(argv)
            if step == "collector stop":
                real(argv, **kwargs)  # the collector stopped, then the command failed
                raise p.Refused("docker failed (exit 1); production was not accepted")
            raise subprocess.TimeoutExpired(argv, 1800)
        return real(argv, **kwargs)
    monkeypatch.setattr(p, "run", flaky)
    with pytest.raises(p.Refused):
        p.apply(host.settings, SHAS[1])
    assert failed and {service: host.containers[service]["Image"] for service in (HOLDER, COLLECTOR)} == previous
    assert all(host.containers[service]["State"]["Running"] for service in (HOLDER, COLLECTOR))
    assert host.ups()[-2:] == [HOLDER, COLLECTOR]


# spec: ANYM-896
def test_a_holder_only_attach_failure_beside_a_pair_restores_the_stopped_collector_and_records_unhealthy(
        tmp_path, monkeypatch):
    host = first(tmp_path, monkeypatch, ("1", "2"))
    previous = {service: host.containers[service]["Image"] for service in (HOLDER, COLLECTOR, "whatsapp2-holder")}
    host.change_holder("serve 2")  # both holders change; only whatsapp1's collector changes too (its definition)
    host.units[COLLECTOR]["environment"] = {"SYNTHETIC": "changed"}
    host.write_compose()
    host.silent.add("whatsapp2-holder")
    with pytest.raises(p.Refused, match="rollback --sha"):
        released(host, SHAS[1], wita(11, 3, 40))
    assert host.containers[COLLECTOR]["State"]["Running"] and host.containers[COLLECTOR]["Image"] == previous[COLLECTOR]
    assert p.read(host.state / SHAS[1] / "result.json")["status"] == "unhealthy" and p.unfinished(host.state) == []
    host.silent.clear()
    p.rollback(host.settings, SHAS[1])
    assert {service: host.containers[service]["Image"] for service in previous} == previous


# spec: ANYM-896
def test_a_window_that_closes_before_the_stop_defers_the_planned_holder_without_counting_it(tmp_path, monkeypatch):
    host = first(tmp_path, monkeypatch)
    host.change_holder("serve 2")
    host.now, host.order, host.main = wita(11, 4, 59), [], SHAS[1]
    p.prepare(host.settings, SHAS[1])
    locks = p.provide_session_locks
    def late(*args, **kwargs):  # in apply, between the decision and the project's stop, the window ends
        host.now = wita(11, 5, 1)
        return locks(*args, **kwargs)
    monkeypatch.setattr(p, "provide_session_locks", late)
    result = p.apply(host.settings, SHAS[1])
    assert host.stopped() == [] and host.outcome(result, HOLDER)["deferred"]["next_window"].endswith("2026-11-12 03:00-05:00 WITA")
    assert [event for event in host.ledger()["events"] if event["release"] == SHAS[1]] == []
    assert "synthetic/" + HOLDER in host.ledger()["pending"]


# spec: ANYM-896
def test_a_new_collector_that_cannot_speak_to_a_holder_kept_for_the_window_is_refused_before_any_stop(
        tmp_path, monkeypatch):
    host = first(tmp_path, monkeypatch)
    host.change_holder("serve 2")
    host.files["whatsapp/whatsmeow/main.go"] = ("100644", "package main // protocol 2\n")
    host.ranges = {"holder": ("1", "2"), "collector": ("2", "2")}  # the new pair speaks 2; the running holder only 1
    host.now, host.order, host.main = wita(10, 10), [], SHAS[1]
    p.prepare(host.settings, SHAS[1])
    with pytest.raises(p.Refused, match="session_pair_incompatible: the new synthetic/whatsapp1 cannot speak"):
        p.apply(host.settings, SHAS[1])
    assert host.order == [] and host.ledger()["events"][-1]["release"] == SHAS[0]
    host.now = wita(11, 3, 40)  # inside the window the pair goes together
    p.apply(host.settings, SHAS[1])
    assert [entry for entry in host.order if entry[1] in (HOLDER, COLLECTOR)] == [
        ("stop", COLLECTOR), ("stop", HOLDER), ("up", HOLDER), ("up", COLLECTOR)]


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
def test_a_pass_whose_call_wait_returns_after_05_00_stops_nothing_and_counts_nothing(tmp_path, monkeypatch):
    host = deferred_host(tmp_path, monkeypatch)
    def calls(settings, deadline):  # the last poll started before 05:00 and answered after it
        host.now = wita(11, 5, 0, ) + timedelta(seconds=2)
    monkeypatch.setattr(p, "wait_for_calls", calls)
    host.now, host.order = wita(11, 4, 59), []
    result = p.holder_pass(host.settings, None)
    assert host.order == [] and result["status"] == "deferred"
    assert result["outcomes"]["synthetic/" + HOLDER] == {"status": "deferred", "services": [HOLDER], "reason": "window closed"}
    assert result["counts"] == {"planned": 0, "recovery": 0, "flagged": 0, "rollback": 0}
    assert "synthetic/" + HOLDER in host.ledger()["pending"] and p.unfinished(host.state) == []


# spec: ANYM-896
def test_a_log_read_that_times_out_in_the_pass_restores_the_holder(tmp_path, monkeypatch):
    host = deferred_host(tmp_path, monkeypatch)
    previous = host.containers[HOLDER]["Image"]
    def timed_out(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 30)
    monkeypatch.setattr(p.subprocess, "run", timed_out)
    monkeypatch.setattr(p, "holder_log", HOLDER_LOG)  # the real reader: a timeout is no attach seen
    monkeypatch.setattr(p, "HOLDER_ATTACH_S", 0)
    host.now, host.order = wita(11, 3, 45), []
    result = p.holder_pass(host.settings, None)
    assert result["status"] == "rolled_back" and "no collector attached" in result["outcomes"]["synthetic/" + HOLDER]["check"]
    assert host.containers[HOLDER]["Image"] == previous and p.unfinished(host.state) == []
    def broken(container, since):
        raise RuntimeError("synthetic")
    monkeypatch.setattr(p, "holder_log", broken)  # any other exception of a check restores too
    host.now = wita(12, 3, 45)
    again = p.holder_pass(host.settings, None)
    assert again["outcomes"]["synthetic/" + HOLDER]["check"] == "a session check failed: RuntimeError"
    assert host.containers[HOLDER]["Image"] == previous


# spec: ANYM-896
def test_the_urgent_flag_is_refused_while_the_compose_configuration_drifted(tmp_path, monkeypatch):
    host = first(tmp_path, monkeypatch)
    http = {"state_directory": str(tmp_path / "http-state")}
    (tmp_path / "http-state").mkdir()
    p.write(tmp_path / "http-state" / "http-current.json", {"config_digest": p.digest(http)})
    host.units[HOLDER]["environment"] = {"SYNTHETIC": "edited by hand"}
    host.write_compose()
    host.order = []
    result = p.holder_flagged(host.settings, http, (HOLDER, "incident:synthetic-record"))
    assert result["status"] == "refused" and "Compose configuration of synthetic changed" in result["reason"]
    assert host.order == []


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
