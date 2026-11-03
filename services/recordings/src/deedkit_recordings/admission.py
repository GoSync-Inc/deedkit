"""Egress admission (ANYM-712): the service refuses to bind its port unless the host firewall rules are in force.

Set `RECORDINGS_EGRESS_ADMISSION=required` (the production overlay does; source runs and tests leave it unset) and name the
probe targets in `RECORDINGS_EGRESS_BLOCKED` and `RECORDINGS_EGRESS_OPEN`, comma-separated:

* blocked `IP:PORT`: an address with a known listener on the host, the tailnet or an integration peer. It passes only when
  nothing answers (timeout or no route); a connection or a refusal means the rules are not in force, because a DROP never
  sends a refusal. `dns:IP[:PORT]` is a UDP DNS query that must get no answer.
* open `HOST:PORT`: a public TCP 443 target that must connect. `dns:IP[:PORT]` is a DNS query to the host resolver that
  must be answered.

A missing or empty variable, a malformed target, a blocked target that answers or an open target that fails refuses the
start (exit 78, fail closed). `restart: unless-stopped` turns this into a loop that keeps only this service down.
The probes are the ones of scripts/recordings_egress_check.py `check` (stdlib only); nothing but a TCP handshake and one
DNS query leaves the container, and no provider token is sent.
"""
import ipaddress
import os
import socket
import sys

REQUIRED = 'required'
EXIT_CODE = 78
TIMEOUT = 3.0
_QUERY = bytes.fromhex('d53401000001000000000000076578616d706c6503636f6d0000010001')


def _tcp(target, timeout, literal):
    host, _, port = target.rpartition(':')
    try:
        if literal:
            ipaddress.ip_address(host.strip('[]'))
        address = socket.getaddrinfo(host.strip('[]'), int(port), type=socket.SOCK_STREAM)[0][4]
    except ValueError:
        return 'malformed'
    except (OSError, UnicodeError):
        return 'unresolved'
    try:
        with socket.create_connection((str(address[0]), int(address[1])), timeout=timeout):
            return 'connected'
    except ConnectionRefusedError:
        return 'refused'
    except TimeoutError:
        return 'timeout'
    except OSError:
        return 'unreachable'


def _dns(target, timeout):
    host, port = target, '53'
    if target.startswith('['):
        host, _, rest = target[1:].partition(']')
        port = rest.removeprefix(':') or '53'
    elif target.count(':') == 1:
        host, port = target.split(':')
    try:
        address = ipaddress.ip_address(host)
        number = int(port)
    except ValueError:
        return 'malformed'
    with socket.socket(socket.AF_INET6 if address.version == 6 else socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.settimeout(timeout)
        try:
            udp.connect((str(address), number))
            udp.send(_QUERY)
            return 'answered' if udp.recv(512)[:2] == _QUERY[:2] else 'malformed'
        except ConnectionRefusedError:
            return 'refused'
        except TimeoutError:
            return 'timeout'
        except OSError:
            return 'unreachable'


def probe(target, expect, timeout=TIMEOUT):
    """(outcome, passed) for one `blocked` or `open` target."""
    dns = target.startswith('dns:')
    body = target[4:] if dns else target
    outcome = _dns(body, timeout) if dns else _tcp(body, timeout, literal=expect == 'blocked')
    if expect == 'open':
        return outcome, outcome == ('answered' if dns else 'connected')
    return outcome, outcome in ('timeout', 'unreachable')


def targets(value):
    return [item.strip() for item in (value or '').split(',') if item.strip()]


def failures(environ=None, timeout=TIMEOUT):
    """The reasons the service must not start; empty when admission is not required or all probes pass."""
    environ = os.environ if environ is None else environ
    if environ.get('RECORDINGS_EGRESS_ADMISSION') != REQUIRED:
        return []
    blocked, opened = targets(environ.get('RECORDINGS_EGRESS_BLOCKED')), targets(environ.get('RECORDINGS_EGRESS_OPEN'))
    reasons = []
    if not blocked:
        reasons.append('RECORDINGS_EGRESS_BLOCKED is missing')
    if not opened:
        reasons.append('RECORDINGS_EGRESS_OPEN is missing')
    if reasons:
        return reasons
    for expect, items in (('blocked', blocked), ('open', opened)):
        for item in items:
            outcome, passed = probe(item, expect, timeout)
            if not passed:
                reasons.append(f'{expect} target {item}: {outcome}')
    return reasons


def require(environ=None, timeout=TIMEOUT):
    """Exit 78 with the reasons (target names and outcomes only, never a token) unless admission passes."""
    reasons = failures(environ, timeout)
    if reasons:
        print('egress admission refused: ' + '; '.join(reasons), file=sys.stderr, flush=True)
        raise SystemExit(EXIT_CODE)
