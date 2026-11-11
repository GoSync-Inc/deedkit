#!/usr/bin/python3 -I
"""Hourly backup alert checker: a failed, undelivered or unverified daily backup becomes a Tiden issue (ANYM-807), and so
does an autodeploy halt on the production host (ci-deploy-speed 11-09: a halt waited until a person happened to look).

Standard library only and no package import, so the same file runs as a host copy
(``/usr/bin/python3 -I /opt/deedkit/backup-health/backup_health.py --config <file>``) on the production host (side
``producer``: the runners' units and ``status.json``) and on the backup server (side ``verifier``: the verifiers' units
and directories). It is read-only towards every backup: no lock, no container, one ``systemctl show`` and file reads.
Events are built like telemetry.py's ``safe_event`` and posted the way tiden_telemetry 0.1.0 posts them; they carry a
fixed code and nothing else (TELEMETRY.md, Backup alerts). Exit codes: 0 checked, 1 unavailable, 2 refused config.
"""
import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid

PROFILES = ('messaging', 'calendar', 'recordings', 'calls')
KINDS = {'producer': ('backup_failed', 'backup_stale'), 'verifier': ('backup_verify_failed', 'backup_unverified')}
CODES = frozenset(f'{kind}.{profile}' for kinds in KINDS.values() for kind in kinds for profile in PROFILES)
# The deployer's halt codes (scripts/production.py HALTS; a test keeps them equal) and `other` for one this copy does
# not know. Only a pause `deploy` wrote names a code; a hand-written one raises nothing: its writer is on the host.
HALT_CODES = ('attempt_unresolved', 'call_wait_exhausted', 'configuration_drift', 'copies_unreconciled',
              'deploy_receipt_malformed', 'deploy_refused', 'deploy_unknown_state', 'enroll_required',
              'host_locks_failed', 'http_release_unsettled', 'http_retirement_failed', 'main_unreadable',
              'manifest_invalid', 'migration_by_hand', 'migration_refused', 'migration_unsettled',
              'migration_wait_exhausted', 'plan_a_preparation_unfinished', 'plan_a_refused', 'plan_a_rolled_back',
              'plan_a_unknown_state', 'plan_b_preparation_unfinished', 'plan_b_stopped', 'registry_unreachable',
              'release_not_applied', 'release_not_on_main', 'rollback_failed', 'session_pass_failed',
              'tool_blob_mismatch', 'tool_by_hand', 'tool_not_descended', 'tool_release_mismatch', 'tool_rolled_back',
              'tool_self_test_failed', 'tool_switch_failed', 'tool_update_stopped', 'tool_update_unresolved', 'other')
DEPLOY_CODES = frozenset(f'deploy_halted.{code}' for code in HALT_CODES)
PAUSE_FILE = '/etc/deedkit/autodeploy.pause'
# Incident 11-11: the receiver's helper stayed at schema 67 (its update follow never enabled) while production moved to
# 84, and every messaging verification failed for two days. On the backup server, component `backup`.
RECEIVER_CODES = frozenset({'backup_receiver_behind'})
FOLLOW_STATUS = 'follow-status.json'
# The follow runs an hour after its previous run ends (BACKUP_R2.md, Receiver update follow); a day plus margin.
FOLLOW_LIMIT_HOURS = 26
FOLLOW_PROGRESS = 'follow.json'
# The profiles whose `restore_schema_failed` can only mean a helper older than the bundle.
LAG_PROFILES = ('messaging', 'recordings')
# Policy limits from capture, the normal day plus margin (ANYM-807 criterion 6); BACKUP.md, Backup alerts. Calls 30 h, not
# 36 (11-09): a missed night shows the next morning; the 04:15 catch-up plus 2 h of verification is 29.5 h.
LIMIT_HOURS = {'messaging': 30, 'calendar': 32, 'recordings': 30, 'calls': 30}
# The proof of these profiles carries the capture time; messaging's and calendar's take the backup ID's job time.
PROOF_CAPTURE = frozenset({'recordings', 'calls'})
FUTURE_GRACE_S = 300
REPEAT_S = 86400
PROBE_TIMEOUT_S = 10
SEND_TIMEOUT_S = 5
SEND_DEADLINE_S = 45
READ_LIMIT = 1 << 20
FRAME = 'services/messaging/messaging_service/backup_health.py'
SDK = {'name': 'tiden.python', 'version': '0.1.0'}
CONTENT_TYPE = 'application/x-tiden-envelope'
ENVIRONMENTS = ('production', 'local-synthetic')
CONFIG_KEYS = {'side', 'environment', 'release_sha', 'dsn_file', 'state_directory', 'profiles'}
ENTRY_KEYS = {'producer': {'units', 'status'}, 'verifier': {'unit', 'directory'}}
UNIT = re.compile(r'[A-Za-z0-9@_.:-]{1,128}\.service')
TIME_FORMAT = '%Y-%m-%dT%H:%M:%SZ'
UNAVAILABLE = 'backup_health_unavailable'


class ConfigError(ValueError):
    pass


def load_config(path):
    """The validated host configuration; ConfigError before any other read."""
    try:
        with open(path, 'rb') as handle:
            data = json.loads(handle.read(READ_LIMIT))
    except (OSError, ValueError, RecursionError):
        raise ConfigError('configuration unreadable') from None
    if not isinstance(data, dict) or set(data) - {'follow'} != CONFIG_KEYS:
        raise ConfigError('configuration keys')
    side, environment, sha = data['side'], data['environment'], data['release_sha']
    if type(side) is not str or side not in KINDS or type(environment) is not str or environment not in ENVIRONMENTS:
        raise ConfigError('side or environment')
    if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{40}', sha):
        raise ConfigError('release_sha')
    # Optional, backup server only: also watch the receiver update follow's status (incident 11-11).
    if 'follow' in data and (side != 'verifier' or type(data['follow']) is not bool):
        raise ConfigError('follow')
    for key in ('dsn_file', 'state_directory'):
        if not isinstance(data[key], str) or not data[key].startswith('/'):
            raise ConfigError(key)
    profiles = data['profiles']
    if not isinstance(profiles, dict) or not profiles or not set(profiles) <= set(PROFILES):
        raise ConfigError('profiles')
    for entry in profiles.values():
        if not isinstance(entry, dict) or set(entry) != ENTRY_KEYS[side]:
            raise ConfigError('profile entry')
        units = entry['units'] if side == 'producer' else [entry['unit']]
        if not isinstance(units, list) or not units or not all(isinstance(u, str) and UNIT.fullmatch(u) for u in units):
            raise ConfigError('units')
        place = entry['status' if side == 'producer' else 'directory']
        if not isinstance(place, str) or not place.startswith('/'):
            raise ConfigError('path')
    return data


def parse_time(value):
    """A timezone-aware ISO 8601 time, or None."""
    if not isinstance(value, str) or not 10 < len(value) <= 64:
        return None
    try:
        moment = datetime.fromisoformat(value[:-1] + '+00:00' if value.endswith('Z') else value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def read_object(path):
    """The JSON object in `path`, None when absent; ValueError for anything unreadable."""
    try:
        with open(path, 'rb') as handle:
            raw = handle.read(READ_LIMIT + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(type(exc).__name__) from None
    if len(raw) > READ_LIMIT:
        raise ValueError('file too large')
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        raise ValueError('not JSON') from None
    if not isinstance(data, dict):
        raise ValueError('not a JSON object')
    return data


def probe_units(units, run=None):
    """{unit: (ActiveState, StateChangeTimestamp or None)} from one `systemctl show`; None when the probe failed."""
    run = run or subprocess.run
    try:
        done = run(['systemctl', 'show', '-p', 'Id', '-p', 'ActiveState', '-p', 'StateChangeTimestamp',
                    '--timestamp=us+utc', '--', *units], capture_output=True, text=True, timeout=PROBE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    found = {}
    for block in done.stdout.strip().split('\n\n'):
        fields = dict(line.split('=', 1) for line in block.splitlines() if '=' in line)
        stamp = fields.get('StateChangeTimestamp', '')
        try:
            moment = datetime.strptime(stamp, '%a %Y-%m-%d %H:%M:%S.%f UTC').replace(tzinfo=timezone.utc) if stamp else None
        except ValueError:
            return None
        found[fields.get('Id')] = (fields.get('ActiveState'), moment)
    return found if all(unit in found for unit in units) else None


def age_of(moment, now, limit):
    """(stale, age in hours or None): a missing or future (beyond the grace) time is stale."""
    if moment is None:
        return True, None
    seconds = (now - moment).total_seconds()
    if seconds < -FUTURE_GRACE_S:
        return True, None
    return seconds > limit * 3600, round(max(seconds, 0) / 3600, 1)


def producer(profile, entry, states, now):
    """{'raised', 'unknown' (kinds not evaluable), 'word', 'last_snapshot_at', 'age_hours'} for one runner."""
    raised, unknown = set(), set()
    try:
        status, reason = read_object(entry['status']), None
    except ValueError as exc:
        status, reason = None, str(exc)
    status = status or {}
    success = parse_time(status.get('last_success_at'))
    failed_units = states is not None and any(
        state == 'failed' and (success is None or (moment is not None and moment >= success))
        for state, moment in (states[u] for u in entry['units']))
    if status.get('state') == 'error' or failed_units:
        raised.add('backup_failed')
    elif states is None or reason:
        unknown.add('backup_failed')
    last = status.get('last_snapshot_at')
    stale, age = age_of(parse_time(last), now, LIMIT_HOURS[profile])
    if stale:
        raised.add('backup_stale')
    word = ('FAILED' if 'backup_failed' in raised else 'STALE' if stale
            else 'UNKNOWN' if unknown else 'delivered')
    # The runner writes microseconds and +00:00; the result carries whole seconds in UTC, as `status` prints it.
    captured = parse_time(last)
    try:
        shown = captured.astimezone(timezone.utc).strftime(TIME_FORMAT) if captured else None
    except (OverflowError, ValueError):  # a time past year 9999 in UTC: already stale, shown as none
        shown = None
    return {'raised': raised, 'unknown': unknown, 'word': word, 'last_snapshot_at': shown, 'age_hours': age}


def verified_capture(profile, directory, success):
    """The capture time of the last verified backup (criterion 5), or None."""
    if success is None or success.get('status') != 'verified':
        return None
    if profile in PROOF_CAPTURE:
        run = success.get('verification_id')
        if not isinstance(run, str) or not re.fullmatch('[a-f0-9]{32}', run):
            return None
        proof = read_object(Path(directory) / f'proof-{run}.json') or {}
        verification = proof.get('verification')
        if not isinstance(verification, dict) or verification.get('id') != run or verification.get('profile') != profile:
            return None
        return parse_time(verification.get('captured_at'))
    # The job's creation time, which a resumed job keeps: never later than the capture (conservative).
    match = re.fullmatch(r'(\d{8}T\d{6}Z)-[0-9a-f]{16}', str(success.get('backup_id')))
    return datetime.strptime(match[1], '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc) if match else None


def verifier(profile, entry, states, now):
    """The verifier side's reading for one profile, shaped as producer()'s."""
    raised, unknown, directory = set(), set(), Path(entry['directory'])
    try:
        success, failure = read_object(directory / 'last-success.json'), read_object(directory / 'last-failure.json')
        captured = verified_capture(profile, directory, success)
        readable = True
    except ValueError:
        success, failure, captured, readable = None, None, None, False
    verified_at = parse_time((success or {}).get('verified_at'))
    failed_at = parse_time((failure or {}).get('failed_at'))
    unit_failed = states is not None and states[entry['unit']][0] == 'failed'
    if unit_failed or (failed_at is not None and (verified_at is None or failed_at > verified_at)):
        raised.add('backup_verify_failed')
    elif states is None or not readable:
        unknown.add('backup_verify_failed')
    stale, age = age_of(captured, now, LIMIT_HOURS[profile])
    if stale:
        raised.add('backup_unverified')
    word = ('FAILED' if 'backup_verify_failed' in raised else 'STALE' if stale
            else 'UNKNOWN' if unknown else 'verified')
    return {'raised': raised, 'unknown': unknown, 'word': word,
            'last_snapshot_at': captured.strftime(TIME_FORMAT) if captured else None, 'age_hours': age}


def halted(path=None):
    """`deploy_halted.<code>` while the autodeploy pause names a code, else None (no pause, a hand-written one, or a
    file that cannot be read). Only the code leaves the host, never the pause's reason."""
    try:
        with open(path or PAUSE_FILE, 'rb') as handle:
            pause = json.loads(handle.read(READ_LIMIT))
    except (OSError, ValueError, RecursionError):
        return None
    code = pause.get('code') if isinstance(pause, dict) else None
    if type(code) is not str:
        return None
    return 'deploy_halted.' + (code if code in HALT_CODES else 'other')


def follow_progress(state_directory, status, now, write):
    """When the follow last proved the pins (`current` or `switched`), kept by this checker in `follow.json`, since
    the follow's own status only says when it last ran. A `waiting` with no record starts the clock now."""
    path = Path(state_directory) / FOLLOW_PROGRESS
    try:
        last = parse_time((read_object(path) or {}).get('following_at'))
    except ValueError:
        last = None
    if last is not None and status not in ('current', 'switched') and (now - last).total_seconds() >= -FUTURE_GRACE_S:
        return last
    if write:
        write_json(path, {'v': 1, 'following_at': now.isoformat()}, 0o600)
    return now


def receiver_behind(config, now, write=False):
    """`backup_receiver_behind` when the newest messaging or recordings verification failure is their helper refusing
    a newer bundle schema (`restore_schema_failed`; calendar's code of that name is a restored-schema mismatch), or,
    with `follow: true` in the configuration, when the receiver update follow (ANYM-597; its status in messaging's
    verifier directory) is missing, unreadable, refused, has not run within FOLLOW_LIMIT_HOURS, or has only waited
    (`verification_pending` behind a stuck retry journal) for that long since it last proved the pins; else None. An
    unreadable verifier file is verifier()'s to report. `write` keeps the follow's progress time (not under --print)."""
    code, = RECEIVER_CODES
    entries = config['profiles']
    for profile in LAG_PROFILES:
        if profile not in entries:
            continue
        try:
            directory = Path(entries[profile]['directory'])
            success, failure = read_object(directory / 'last-success.json'), read_object(directory / 'last-failure.json')
        except ValueError:
            continue
        failed_at = parse_time((failure or {}).get('failed_at'))
        verified_at = parse_time((success or {}).get('verified_at'))
        if ((failure or {}).get('error') == 'restore_schema_failed' and failed_at is not None and
                (verified_at is None or failed_at > verified_at)):
            return code
    if not config.get('follow') or 'messaging' not in entries:
        return None
    try:
        follow = read_object(Path(entries['messaging']['directory']) / FOLLOW_STATUS) or {}
    except ValueError:
        return code
    checked, status = parse_time(follow.get('checked_at')), follow.get('status')
    if status not in ('current', 'switched', 'waiting') or checked is None:
        return code
    age = (now - checked).total_seconds()
    if age > FOLLOW_LIMIT_HOURS * 3600 or age < -FUTURE_GRACE_S:
        return code
    since = follow_progress(config['state_directory'], status, now, write)
    return code if (now - since).total_seconds() > FOLLOW_LIMIT_HOURS * 3600 else None


def evaluate(config, now, run=None):
    """{profile: reading}; one profile's failure leaves it UNKNOWN and the others evaluated."""
    side, entries = config['side'], config['profiles']
    units = sorted({u for e in entries.values() for u in (e['units'] if side == 'producer' else [e['unit']])})
    states = probe_units(units, run)
    readings = {}
    for profile, entry in entries.items():
        try:
            readings[profile] = (producer if side == 'producer' else verifier)(profile, entry, states, now)
        except Exception:
            readings[profile] = {'raised': set(), 'unknown': set(KINDS[side]), 'word': 'UNKNOWN',
                                 'last_snapshot_at': None, 'age_hours': None}
    return readings


def line(profile, reading, checked):
    age = reading.get('age_hours')
    shown = f'{age:.1f}h' if isinstance(age, (int, float)) and not isinstance(age, bool) else '?'
    return (f"backup {profile}: {reading['word']} last capture {reading.get('last_snapshot_at') or 'never'} "
            f"age={shown} (checked {checked})")


def ingest_url(dsn):
    """tiden_telemetry 0.1.0's `parse_dsn`, after telemetry.py's DSN rule (https, a user, no password)."""
    url = urllib.parse.urlsplit(dsn)
    if url.scheme != 'https' or not url.username or url.password or not url.hostname:
        raise ValueError('invalid_telemetry_dsn')
    project = url.path.lstrip('/').split('/', 1)[0]
    if not project:
        raise ValueError('invalid_telemetry_dsn')
    host = url.hostname + (f':{url.port}' if url.port is not None else '')
    return (f"https://{host}/api/{urllib.parse.quote(project, safe='')}"
            f"/envelope/?tiden_key={urllib.parse.quote(url.username, safe='')}")


def build_event(code, config, timestamp):
    """The event telemetry.safe_event + before_send produce for this code (ANYM-807 criterion 8)."""
    if code not in CODES and code not in DEPLOY_CODES and code not in RECEIVER_CODES:
        raise ValueError('unknown code')
    component = 'deploy' if code in DEPLOY_CODES else 'backup'
    try:
        raise RuntimeError(code)
    except RuntimeError as error:
        trace = error.__traceback__
        lineno = trace.tb_lineno if trace is not None else 1
    return {'platform': 'python', 'level': 'error', 'release': 'deedkit@' + config['release_sha'],
            'tags': {'component': component, 'error_code': code},
            'exception': {'values': [{'type': 'RuntimeError', 'value': code,
                                      'stacktrace': {'frames': [{'filename': FRAME, 'lineno': lineno, 'in_app': True}]}}]},
            'fingerprint': ['backup-health', component, code],
            'event_id': uuid.uuid4().hex, 'timestamp': timestamp, 'environment': config['environment'], 'sdk': dict(SDK)}


def envelope(event):
    """tiden_telemetry 0.1.0's `serialize_envelope`."""
    body = json.dumps(event, separators=(',', ':'), ensure_ascii=False, default=str).encode('utf-8')
    header = json.dumps({'event_id': event.get('event_id'), 'sent_at': time.strftime(TIME_FORMAT, time.gmtime())},
                        separators=(',', ':')).encode('utf-8')
    item = json.dumps({'type': 'event', 'length': len(body), 'content_type': 'application/json'},
                      separators=(',', ':')).encode('utf-8')
    return header + b'\n' + item + b'\n' + body + b'\n'


def post(url, data, timeout):
    """True only on an HTTP 2xx."""
    request = urllib.request.Request(url, data=data, method='POST', headers={'Content-Type': CONTENT_TYPE})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read(4096)
            return 200 <= response.status < 300
    except Exception:  # an HTTP protocol error (IncompleteRead, BadStatusLine) is a failed send like any other
        return False


def bounded(send, url, data, timeout):
    """`send` with a limit on its whole duration: a response that trickles in counts as failed once `timeout` passes.
    The daemon thread of an abandoned send ends with the process."""
    outcome = []

    def attempt():
        try:
            outcome.append(bool(send(url, data, timeout)))
        except Exception:
            outcome.append(False)
    worker = threading.Thread(target=attempt, name='backup-health-send', daemon=True)
    worker.start()
    worker.join(timeout)
    return bool(outcome and outcome[0])


def moment_value(value):
    """A stored epoch time, or None for anything else (bool, non-finite, out of range)."""
    if type(value) not in (int, float):
        return None
    try:
        value = float(value)
    except OverflowError:
        return None
    return value if math.isfinite(value) and 0 <= value < 1e11 else None


def read_state(path):
    try:
        data = read_object(path)
    except ValueError:
        return {}
    codes = (data or {}).get('codes')
    if not isinstance(codes, dict):
        return {}
    good = {}
    for code, entry in codes.items():
        if (code not in CODES and code not in DEPLOY_CODES and code not in RECEIVER_CODES) or not isinstance(entry, dict):
            continue
        sent, attempted = (moment_value(entry.get(k)) for k in ('sent', 'attempted'))
        if (sent is None) != (entry.get('sent') is None) or (attempted is None) != (entry.get('attempted') is None):
            continue  # a malformed entry is dropped: its code is due again
        good[code] = {'sent': sent, 'attempted': attempted}
    return good


def write_json(path, data, mode):
    path = Path(path)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f'.{path.name}.')
    try:
        with os.fdopen(descriptor, 'w') as handle:
            handle.write(json.dumps(data, sort_keys=True) + '\n')
            os.fchmod(handle.fileno(), mode)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    except BaseException:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def active_codes(readings, previous):
    """Raised codes, plus the codes of an episode whose kind could not be evaluated this time."""
    active = set()
    for profile, reading in readings.items():
        active |= {f'{kind}.{profile}' for kind in reading['raised']}
        active |= {f'{kind}.{profile}' for kind in reading['unknown'] if f'{kind}.{profile}' in previous}
    return active


def due_codes(active, state, now):
    """Codes to send now, least recently attempted first."""
    due = [c for c in active if state.get(c, {}).get('sent') is None or now - state[c]['sent'] >= REPEAT_S]
    return sorted(due, key=lambda c: (state.get(c, {}).get('attempted') is not None, state.get(c, {}).get('attempted') or 0, c))


def check(config, *, now=None, clock=time.monotonic, run=None, send=None, wall=time.time):
    """One run: evaluate, write result.json, send due events within the deadline, write the episode state."""
    moment, send = now or datetime.now(timezone.utc), send or post
    readings = evaluate(config, moment, run)
    state_dir = Path(config['state_directory'])
    checked = moment.strftime(TIME_FORMAT)
    write_json(state_dir / 'result.json', {'v': 1, 'side': config['side'], 'checked_at': checked, 'profiles': {
        p: {k: r[k] for k in ('word', 'last_snapshot_at', 'age_hours')} for p, r in readings.items()}}, 0o644)
    previous = read_state(state_dir / 'state.json')
    active = active_codes(readings, previous)
    if config['side'] == 'producer' and (code := halted()):
        active.add(code)
    if config['side'] == 'verifier':
        try:
            behind = receiver_behind(config, moment, write=True)
        except Exception:
            behind = next((c for c in RECEIVER_CODES if c in previous), None)
        if behind:
            active.add(behind)
    state = {code: previous.get(code, {'sent': None, 'attempted': None}) for code in active}
    stamp = moment.timestamp()
    due = due_codes(active, state, stamp)
    if due:
        try:
            with open(config['dsn_file']) as handle:
                url = ingest_url(handle.read(4096).strip())
        except (OSError, ValueError):
            url = None
        deadline = clock() + SEND_DEADLINE_S
        for code in due:
            remaining = deadline - clock()
            if url is None or remaining <= 0:
                break
            state[code]['attempted'] = stamp
            if bounded(send, url, envelope(build_event(code, config, wall())), min(SEND_TIMEOUT_S, remaining)):
                state[code]['sent'] = stamp
    write_json(state_dir / 'state.json', {'v': 1, 'codes': state}, 0o600)
    return readings, due


def main(argv=None, out=print):
    parser = argparse.ArgumentParser(description='Raise a Tiden issue for a failed, undelivered or unverified backup.')
    parser.add_argument('--config', required=True)
    parser.add_argument('--print', action='store_true', help='print the per-profile lines; send and write nothing')
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f'backup_health: refused configuration ({exc})', file=sys.stderr)
        return 2
    try:
        if args.print:
            now = datetime.now(timezone.utc)
            for profile, reading in evaluate(config, now).items():
                out(line(profile, reading, now.strftime(TIME_FORMAT)))
            if config.get('follow'):
                out(f"backup receiver: {'BEHIND' if receiver_behind(config, now) else 'following'} "
                    f"(checked {now.strftime(TIME_FORMAT)})")
            return 0
        readings, due = check(config)
        out(json.dumps({'checked': sorted(readings), 'due': sorted(due)}))
        return 0
    except Exception:
        print(UNAVAILABLE, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
