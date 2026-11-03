"""The recordings backend's host rules and egress check (ANYM-712); local sockets only, no firewall is changed on the host."""
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import threading
import time

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'scripts'
SCRIPT = SCRIPTS / 'recordings_egress_check.py'
INFERENCE = SCRIPTS / 'inference_egress_check.py'
DELIVERY = SCRIPTS / 'delivery_egress_check.py'
PERSIST = SCRIPTS / 'firewall_persist.py'
RESOLVER = '192.0.2.53'
MINI = '100.101.102.103'
CHAIN = 'DEEDKIT-RECORDINGS'
BRIDGE = 'dk-recordings0'
BACKEND = 'dk-recbackend0'
CANARY_IP = '192.0.2.254'
DROPPED = ('0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8', '169.254.0.0/16', '172.16.0.0/12',
           '192.0.0.0/24', '192.168.0.0/16', '198.18.0.0/15', '224.0.0.0/4', '240.0.0.0/4')


def run_script(script, *args, stdin=None):
    result = subprocess.run([sys.executable, '-I', str(script), *args], text=True, capture_output=True, timeout=60, input=stdin)
    return result.returncode, result.stdout, result.stderr


def run(*args):
    return run_script(SCRIPT, *args)


def commands(*extra):
    code, out, err = run('rules', '--resolver', RESOLVER, *extra)
    assert code == 0, err
    return out.splitlines()


BACKEND_RULES = [f'-i {BACKEND} -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN', f'-i {BACKEND} -p tcp --dport 13234 -j RETURN',
                 f'-i {BACKEND} -j DROP']


def golden():
    egress = ([f'iptables -A {CHAIN} -d {RESOLVER} -p {proto} --dport 53 -j RETURN' for proto in ('udp', 'tcp')] +
              [f'iptables -A {CHAIN} -d {network} -j DROP' for network in DROPPED] +
              [f'iptables -A {CHAIN} -p tcp --dport 443 -j RETURN', f'iptables -A {CHAIN} -j DROP'])
    backend = [f'iptables -A {CHAIN} {rule}' for rule in BACKEND_RULES]
    reject = f'iptables -A {CHAIN} -d {CANARY_IP} -p tcp --dport 9 -j REJECT --reject-with tcp-reset'
    drop = f'iptables -A {CHAIN} -d {CANARY_IP} -p tcp --dport 9 -j DROP'
    filter_chain = [f'iptables -N {CHAIN}', f'iptables -F {CHAIN}', *backend, reject, *egress]
    raw_chain = [line.replace('iptables ', 'iptables -t raw ', 1) for line in [f'iptables -N {CHAIN}', f'iptables -F {CHAIN}', drop, *egress]]
    return (filter_chain + raw_chain +
            [f'iptables -I DOCKER-USER -i {BRIDGE} -j {CHAIN}', f'iptables -t raw -I PREROUTING -i {BRIDGE} -j {CHAIN}',
             f'iptables -I DOCKER-USER -i {BACKEND} -j {CHAIN}',
             f'iptables -I INPUT -i {BRIDGE} -j DROP', f'iptables -I INPUT -i {BACKEND} -j DROP',
             f'ip6tables -I DOCKER-USER -i {BRIDGE} -j DROP', f'ip6tables -t raw -I PREROUTING -i {BRIDGE} -j DROP',
             f'ip6tables -I INPUT -i {BRIDGE} -j DROP', f'ip6tables -I INPUT -i {BACKEND} -j DROP'])


# spec: ANYM-712
def test_rules_print_the_golden_list_for_the_filter_and_the_raw_table():
    assert commands() == golden()
    assert run('rules')[0] == 2  # a resolver is required
    # The two hooks into one chain, the host-bound drops for both bridges, IPv6 dropped in all three places.
    lines = commands()
    assert f'iptables -I DOCKER-USER -i {BRIDGE} -j {CHAIN}' in lines and f'iptables -t raw -I PREROUTING -i {BRIDGE} -j {CHAIN}' in lines
    assert sum(line.startswith('ip6tables') for line in lines) == 4 and 'ip6tables -I INPUT -i dk-recbackend0 -j DROP' in lines


def chain_of(lines, table):
    prefix = f'iptables -t raw -A {CHAIN} ' if table == 'raw' else f'iptables -A {CHAIN} '
    return [rule for rule in (line.removeprefix(prefix) for line in lines if line.startswith(prefix)) if f'-i {BACKEND}' not in rule]


def backend_of(lines):
    return [line.removeprefix(f'iptables -A {CHAIN} ') for line in lines if line.startswith(f'iptables -A {CHAIN} -i {BACKEND}')]


def backend_verdict(state, port):
    """The backend rules in order for one packet that enters from the backend bridge: RETURN (Docker's own rules then
    decide) or DROP. `state` is `new` for a connection being opened, `reply` for an established one."""
    for rule in BACKEND_RULES:
        words = rule.split()
        if '--ctstate' in words and state not in ('reply',):
            continue
        if '--dport' in words and int(words[words.index('--dport') + 1]) != port:
            continue
        return words[words.index('-j') + 1]
    return 'RETURN'


def verdict(chain, destination, protocol, port):
    """The first matching rule of the chain for one packet: RETURN (falls back to the caller) or DROP."""
    address = ipaddress.ip_address(destination)
    for rule in chain:
        words = rule.split()
        target = words[words.index('-j') + 1]
        if '-d' in words and address not in ipaddress.ip_network(words[words.index('-d') + 1]):
            continue
        if '-p' in words and words[words.index('-p') + 1] != protocol:
            continue
        if '--dport' in words and int(words[words.index('--dport') + 1]) != port:
            continue
        return target
    return 'RETURN'


BLOCKED = (('172.18.0.1', 'tcp', 8080), ('100.64.7.7', 'tcp', 443), ('169.254.169.254', 'tcp', 80), ('10.0.0.5', 'tcp', 443),
           ('192.168.1.10', 'tcp', 443), ('127.0.0.1', 'tcp', 443), ('203.0.113.9', 'tcp', 22), ('203.0.113.9', 'tcp', 80),
           ('203.0.113.9', 'udp', 443), ('198.51.100.7', 'udp', 53), ('224.0.0.251', 'tcp', 443), ('172.20.0.2', 'tcp', 443))
OPEN = ((RESOLVER, 'udp', 53), (RESOLVER, 'tcp', 53), ('203.0.113.9', 'tcp', 443), ('198.51.100.7', 'tcp', 443))


# spec: ANYM-712
def test_every_blocked_network_precedes_the_public_443_allow_in_both_tables_and_the_verdicts_hold():
    lines = commands()
    for table in ('filter', 'raw'):
        chain = chain_of(lines, table)
        allow = chain.index('-p tcp --dport 443 -j RETURN')
        assert chain[-1] == '-j DROP' and allow == len(chain) - 2
        for network in DROPPED:
            assert chain.index(f'-d {network} -j DROP') < allow, (table, network)
        assert chain.index(f'-d {RESOLVER} -p udp --dport 53 -j RETURN') < chain.index('-d 0.0.0.0/8 -j DROP')
        assert all(verdict(chain, *packet) == 'DROP' for packet in BLOCKED), [p for p in BLOCKED if verdict(chain, *p) != 'DROP']
        assert all(verdict(chain, *packet) == 'RETURN' for packet in OPEN)
    assert chain_of(lines, 'filter')[1:] == chain_of(lines, 'raw')[1:]  # the first rule of each is the canary


# spec: ANYM-712
def test_the_canary_is_dropped_by_the_raw_chain_and_refused_by_the_filter_chain():
    lines = commands()
    raw, filtered = chain_of(lines, 'raw'), chain_of(lines, 'filter')
    assert raw[0] == f'-d {CANARY_IP} -p tcp --dport 9 -j DROP'
    assert filtered[0] == f'-d {CANARY_IP} -p tcp --dport 9 -j REJECT --reject-with tcp-reset'
    # TEST-NET-1 (RFC 5737) is not a routable address and is in none of the dropped networks, so only these two rules decide.
    assert ipaddress.ip_address(CANARY_IP) in ipaddress.ip_network('192.0.2.0/24') and ipaddress.ip_address(CANARY_IP).is_private
    assert not any(ipaddress.ip_address(CANARY_IP) in ipaddress.ip_network(network) for network in DROPPED)
    assert verdict(raw, CANARY_IP, 'tcp', 9) == 'DROP' and verdict(filtered, CANARY_IP, 'tcp', 9) == 'REJECT'
    assert 'CANARY = \'192.0.2.254:9\'' in (ROOT / 'services/recordings/src/deedkit_recordings/admission.py').read_text()
    assert not any(CANARY_IP in line for line in lines if 'INPUT' in line or 'ip6tables' in line)


# spec: ANYM-712
def test_the_backend_bridge_lets_a_peer_open_a_connection_only_to_the_service_port_and_replies_pass():
    lines = commands()
    assert backend_of(lines) == BACKEND_RULES and f'iptables -I DOCKER-USER -i {BACKEND} -j {CHAIN}' in lines
    # They come first in the filter chain, are not in the raw chain (no conntrack before it) and the egress rules never see them.
    assert [line for line in lines if line.startswith(f'iptables -A {CHAIN} ')][:3] == [f'iptables -A {CHAIN} {rule}' for rule in BACKEND_RULES]
    assert not any(BACKEND in line for line in lines if line.startswith(f'iptables -t raw -A {CHAIN} '))
    # The gateway's request to recordings-api (new, service port) and the replies pass; the recordings container opening a
    # connection to the gateway (13240) or to any other port of a peer is dropped, and so is a UDP flow of a peer.
    assert backend_verdict('new', 13234) == 'RETURN' and backend_verdict('reply', 51234) == 'RETURN'
    assert [backend_verdict('new', port) for port in (13240, 80, 443, 22, 5432, 53)] == ['DROP'] * 6


# spec: ANYM-712
def test_undo_removes_each_added_rule_hook_and_chain():
    added, removed = commands(), commands('--undo')
    jumps = [line for line in added if ' -I ' in line]
    inverse = [line.replace(' -I ', ' -D ', 1) for line in jumps]
    assert sorted(line for line in removed if ' -D ' in line) == sorted(inverse)
    assert {line for line in removed if ' -D ' not in line} == {
        f'iptables -F {CHAIN}', f'iptables -X {CHAIN}', f'iptables -t raw -F {CHAIN}', f'iptables -t raw -X {CHAIN}'}
    # The chains are deleted after the jumps that use them; nothing of the inference or delivery family appears.
    assert all(removed.index(line) < removed.index(f'iptables -F {CHAIN}') for line in removed if ' -D ' in line)
    assert not [line for line in removed + added if 'INFERENCE' in line or 'DELIVERY' in line or 'dk-inference' in line or 'dk-delivery' in line]
    assert len(set(removed)) == len(removed)


# spec: ANYM-712
def test_the_raw_hook_is_first_after_repeated_loads_and_a_forward_reorder_and_blocked_targets_stay_dropped():
    """A model of the kernel's order: raw PREROUTING before filter FORWARD, `ensure` = check then insert at position 1."""
    prerouting, forward = [], ['DOCKER-USER']
    hook = f'-i {BRIDGE} -j {CHAIN}'

    def ensure(chain, rule):
        if rule not in chain:
            chain.insert(0, rule)

    def load():
        ensure(prerouting, hook)
        ensure(forward, f'-i {BRIDGE} -j {CHAIN}')

    def reorder():
        forward.insert(0, '-j ACCEPT # ts-forward, reinstalled by tailscaled at position 1')

    load()
    reorder()
    load()
    load()
    assert prerouting == [hook] and prerouting.count(hook) == 1
    assert forward[0].endswith('ts-forward, reinstalled by tailscaled at position 1')  # DOCKER-USER can be passed over...
    raw = chain_of(commands(), 'raw')
    # ...so the raw hook decides: it runs first and drops every blocked target whatever FORWARD does afterwards.
    assert prerouting[0] == hook and all(verdict(raw, *packet) == 'DROP' for packet in BLOCKED)


# spec: ANYM-712
def test_the_delivery_and_inference_outputs_are_byte_identical_to_the_base_commit():
    def digest(script, *args):
        code, out, err = run_script(script, *args)
        assert code == 0, err
        return hashlib.sha256(out.encode()).hexdigest()
    inference = ('rules', '--mini-ip', MINI)
    assert digest(INFERENCE, *inference) == 'a6664e911f357d962fe0b856243b33024ee61a210a2c9d79268806fce1e96906'
    assert digest(INFERENCE, 'rules', '--undo') == 'f25f72795e4907a76d3cebfecb3f6127a89836492a9505b3138e8559eb7318e7'
    assert digest(INFERENCE, *inference, '--format', 'ufw', '--family', 'v4') == '8dff6063ab82fe2ca69e7511799b15c3d641892b0542a498fe79dac746e350ce'
    assert digest(INFERENCE, *inference, '--format', 'ufw', '--family', 'v6') == 'a0672b7ca240a683b671acc245af3b31ecd44d8351a11e861cbf188781effdc7'
    assert digest(INFERENCE, *inference, '--format', 'after-init') == 'f3144b4a611dcbe27b2bd0dbabad1b3a15f169163a05520cc619418bd5fcc87c'
    delivery = ('rules', '--resolver', RESOLVER)
    assert digest(DELIVERY, *delivery) == 'b5ae63b35a45bbadf92487b548e3fd903c81a0a10fb079221a9539edd98457c1'
    assert digest(DELIVERY, 'rules', '--undo') == '7b725b138d3c8e7380b25f9aef89ab65a572dc03608bc1e4131bc60b5658f2a7'
    assert digest(DELIVERY, *delivery, '--format', 'ufw', '--family', 'v4') == '3f6dad138323f6752bc30e03b79ccdd1022c6d674113e4167925c3ec9abacce2'
    assert digest(DELIVERY, *delivery, '--format', 'ufw', '--family', 'v6') == '8ec7fa7881e2c69a7742ed5026b2640ec0b7388ad2d4bb3d98b8c1902c82b96c'
    assert run('rules', '--resolver', RESOLVER)[0] == 0  # the module under test exists


TEMPLATE_V4 = '''*filter
:ufw-before-input - [0:0]
:ufw-before-output - [0:0]
:ufw-before-forward - [0:0]
# End required lines
:DOCKER-USER - [0:0]
:DEEDKIT-DELIVERY - [0:0]
-A DEEDKIT-DELIVERY -d 192.0.2.53 -p udp --dport 53 -j RETURN
-A DEEDKIT-DELIVERY -j DROP
-A DOCKER-USER -i dk-delivery0 -j DEEDKIT-DELIVERY
# deedkit-delivery end
-A ufw-before-input -i dk-delivery0 -j DROP
-A ufw-before-input -i lo -j ACCEPT
COMMIT
'''
TEMPLATE_V6 = '''*filter
:ufw6-before-input - [0:0]
:ufw6-before-output - [0:0]
:ufw6-before-forward - [0:0]
# End required lines
:DOCKER-USER - [0:0]
-A DOCKER-USER -i dk-delivery0 -j DROP
-A ufw6-before-input -i dk-delivery0 -j DROP
-A ufw6-before-input -i lo -j ACCEPT
COMMIT
'''


def ufw_files(tmp_path, with_inference=True):
    """before.rules and before6.rules as ufw holds them: the delivery block, then the recordings and the inference blocks."""
    files = {}
    for family, template in (('v4', TEMPLATE_V4), ('v6', TEMPLATE_V6)):
        base = tmp_path / f'base.{family}'
        base.write_text(template)
        current = base
        if with_inference:
            merged = tmp_path / f'inference.{family}'
            code, out, err = run_script(INFERENCE, 'rules', '--mini-ip', MINI, '--format', 'ufw', '--family', family, '--source', str(current))
            assert code == 0, err
            merged.write_text(out)
            current = merged
        full = tmp_path / f'full.{family}'
        code, out, err = run('rules', '--resolver', RESOLVER, '--format', 'ufw', '--family', family, '--source', str(current))
        assert code == 0, err
        full.write_text(out)
        files[family], files[f'base_{family}'] = full, base
    return files


# spec: ANYM-712
def test_the_ufw_block_is_the_filter_and_ipv6_half_of_the_command_form_and_merging_is_idempotent(tmp_path):
    code, v4, err = run('rules', '--resolver', RESOLVER, '--format', 'ufw', '--family', 'v4')
    assert code == 0, err
    lines = v4.splitlines()
    assert lines[0] == '# deedkit-recordings begin' and lines[-1] == '# deedkit-recordings end' and f':{CHAIN} - [0:0]' in lines
    assert '*raw' not in v4 and not any(' -t raw' in line or 'PREROUTING' in line for line in lines)  # the raw half is not in ufw's files
    assert [line for line in lines if line.startswith(f'-A {CHAIN} ')] == [line.removeprefix('iptables ') for line in commands() if line.startswith(f'iptables -A {CHAIN} ')]
    assert f'-A DOCKER-USER -i {BRIDGE} -j {CHAIN}' in lines
    assert f'-A ufw-before-input -i {BRIDGE} -j DROP' in lines and f'-A ufw-before-input -i {BACKEND} -j DROP' in lines
    code, v6, _ = run('rules', '--resolver', RESOLVER, '--format', 'ufw', '--family', 'v6')
    assert code == 0 and v6.splitlines()[1:-1] == [f'-A DOCKER-USER -i {BRIDGE} -j DROP', f'-A ufw6-before-input -i {BRIDGE} -j DROP',
                                                    f'-A ufw6-before-input -i {BACKEND} -j DROP']
    files = ufw_files(tmp_path)
    code, out, err = run('rules', '--resolver', RESOLVER, '--format', 'ufw', '--family', 'v4', '--source', str(files['v4']))
    assert code == 0 and out == files['v4'].read_text(), err
    # The delivery and inference blocks survive the merge unchanged.
    merged = files['v4'].read_text()
    assert all(line in merged.splitlines() for line in TEMPLATE_V4.splitlines())
    assert merged.count('# deedkit-inference begin') == 1 and merged.count('# deedkit-recordings begin') == 1


def hook_text(resolver=RESOLVER, mini=MINI):
    code, out, err = run('rules', '--resolver', resolver, '--mini-ip', mini, '--format', 'after-init')
    assert code == 0, err
    return out


# spec: ANYM-712
def test_the_one_generated_hook_composes_both_families_and_inference_alone_stays_byte_identical():
    composed = hook_text()
    code, inference_only, _ = run_script(INFERENCE, 'rules', '--mini-ip', MINI, '--format', 'after-init')
    assert code == 0 and composed != inference_only
    code, via_inference, err = run_script(INFERENCE, 'rules', '--mini-ip', MINI, '--format', 'after-init', '--recordings-resolver', RESOLVER)
    assert code == 0 and via_inference == composed, err  # both scripts print the same one hook
    assert composed.count('#!/bin/sh') == 1 and composed.count('iptables-restore --noflush -T raw') == 2
    # Own restore of the recordings chain only, the jump after it, every command bounded, the step still exits 0.
    assert composed.index('recordings_step() {') < composed.index('\nstep() {\n')
    body = composed[composed.index('recordings_step() {'):composed.index('\nstep() {\n') + 1]
    assert body.count('timeout -k 2 "$LIMIT" iptables-restore --noflush -T raw') == 1 and '-F ' not in body and ' -X ' not in body
    assert body.index('iptables-restore') < body.index('ensure iptables')
    assert 'ensure iptables PREROUTING -i dk-recordings0 -j DEEDKIT-RECORDINGS || ' in body
    assert 'ensure ip6tables PREROUTING -i dk-recordings0 -j DROP || ' in body
    assert 'record true ok' in composed and 'record false "$REC_FAIL"' in composed and composed.rstrip().endswith('exit 0')
    # The recordings half is a bounded child with its own aggregate budget, so it cannot take the inference half's share
    # of the hook's 60 s: at most 10 s (default 8) plus the 2 s kill grace. Nested commands live in their own process groups,
    # so the child gets the budget as its per-command limit and no pipe of the caller: the result is its exit status.
    assert 'DEEDKIT_STEP_TIMEOUT=$RBUDGET timeout -k 2 "$RBUDGET" sh "$0" recordings </dev/null >/dev/null 2>&1' in composed
    assert 'RBUDGET=${DEEDKIT_RECORDINGS_TIMEOUT:-8}' in composed and '"$RBUDGET" -le 10' in composed
    assert '$(' not in composed[composed.index('RBUDGET='):composed.index('record true ok')]
    assert '11) REC_FAIL=restore_recordings_v4' in composed and '*) REC_FAIL=hook_recordings_$?' in composed
    assert 'TOTAL=60' in composed and 'LIMIT=30' in composed
    # The inference half is the base text with only the recordings additions around it.
    stripped = composed.replace(body, '').replace('    recordings) recordings_step; exit $? ;;\n', '')
    stripped = re.sub(r'    RBUDGET=.*?    esac\n', '', stripped, flags=re.S)
    stripped = re.sub(r'    if \[ -n "\$REC_FAIL" \].*\n', '    record true ok\n', stripped)
    assert stripped == inference_only


def kernel_save(binary, table, extra_commands, drop=()):
    """What `iptables-save -t TABLE` prints for the command forms (nft style: -m tcp, /32), plus unrelated rules."""
    declared = {'iptables': {'filter': [f':{CHAIN} - [0:0]', ':DEEDKIT-INFERENCE - [0:0]'],
                             'raw': [f':{CHAIN} - [0:0]', ':DEEDKIT-INFERENCE - [0:0]']}.get(table, []), 'ip6tables': []}[binary]
    body = []
    for command in extra_commands:
        words = command.split()
        if words[0] != binary:
            continue
        words = words[1:]
        rule_table = 'filter'
        if words[:2] == ['-t', 'raw']:
            rule_table, words = 'raw', words[2:]
        if rule_table != table or words[0] not in ('-A', '-I'):
            continue
        rule = ' '.join(['-A', *words[1:]]).replace(' -p tcp --dport', ' -p tcp -m tcp --dport')
        rule = re.sub(r'(-p udp) --dport', r'\1 -m udp --dport', rule)
        rule = re.sub(r'-d ([0-9.]+) ', r'-d \1/32 ', rule)
        if rule not in drop:
            body.append(rule)
    unrelated = {'filter': ['-A INPUT -i lo -j ACCEPT', '-A DOCKER-USER -j RETURN'], 'raw': ['-A PREROUTING -d 192.0.2.77/32 -j ACCEPT']}
    return '\n'.join([f'*{table}', *declared, *body, *unrelated[table], 'COMMIT', ''])


def inference_commands():
    code, out, err = run_script(INFERENCE, 'rules', '--mini-ip', MINI)
    assert code == 0, err
    return [line for line in out.splitlines() if ' -N ' not in line and ' -F ' not in line]


class Host:
    """A fixture host: ufw files, the composed hook, the env file and an iptables-save pair that cat fixtures."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.files = ufw_files(tmp_path)
        self.hook = tmp_path / 'after.init'
        self.hook.write_text(hook_text())
        self.hook.chmod(0o755)
        self.env = tmp_path / 'env'
        self.env.write_text(f'INFERENCE_MINI_IP={MINI}\nRECORDINGS_RESOLVER={RESOLVER}\n')
        self.receipt = tmp_path / 'firewall-verify.json'
        every = [line for line in commands() if ' -N ' not in line and ' -F ' not in line] + inference_commands()
        self.kernel = {(b, t): kernel_save(b, t, every) for b in ('iptables', 'ip6tables') for t in ('filter', 'raw')}

    def run(self, script=SCRIPT, *extra, boot='boot-1'):
        for binary in ('iptables', 'ip6tables'):
            for table in ('filter', 'raw'):
                (self.tmp / f'{binary}.{table}').write_text(self.kernel[(binary, table)])
            tool = self.tmp / f'{binary}-save'
            tool.write_text(f'#!/bin/sh\ncat "{self.tmp}/{binary}.$2"\n')
            tool.chmod(0o755)
        return run_script(script, 'verify', '--env-file', str(self.env), '--rules-v4', str(self.files['v4']),
                          '--rules-v6', str(self.files['v6']), '--after-init', str(self.hook),
                          '--iptables-save', str(self.tmp / 'iptables-save'), '--ip6tables-save', str(self.tmp / 'ip6tables-save'),
                          '--receipt', str(self.receipt), '--boot-id', boot, *extra)


# spec: ANYM-712
def test_verify_is_ok_when_repository_files_hook_and_kernel_agree_and_inference_stays_ok_with_the_composed_hook(tmp_path):
    host = Host(tmp_path)
    assert host.run()[:2] == (0, 'ok\n'), host.run()
    assert host.run(INFERENCE)[:2] == (0, 'ok\n'), host.run(INFERENCE)
    # ufw's own host-bound chain holds the drop in the files and the kernel alike.
    host.kernel[('iptables', 'filter')] = host.kernel[('iptables', 'filter')].replace(
        f'-A INPUT -i {BRIDGE}', f'-A ufw-before-input -i {BRIDGE}').replace(
        f'-A INPUT -i {BACKEND}', f'-A ufw-before-input -i {BACKEND}').replace('COMMIT', '-A INPUT -j ufw-before-input\nCOMMIT')
    assert host.run()[:2] == (0, 'ok\n')


# spec: ANYM-712
def test_verify_gives_a_code_for_a_dropped_filter_block_a_kernel_mismatch_an_ipv6_gap_and_a_stale_hook(tmp_path):
    # A ufw reload that dropped the filter block: the file loses it, the kernel (loaded before) still has it.
    host = Host(tmp_path)
    host.files['v4'].write_text(host.files['base_v4'].read_text())
    code, out, _ = host.run()
    assert code == 1 and out.splitlines()[0].startswith('file_vs_repo ') and out.splitlines()[0] != 'file_vs_repo ok', out
    # The kernel lost the filter hook.
    host = Host(tmp_path)
    host.kernel[('iptables', 'filter')] = host.kernel[('iptables', 'filter')].replace(f'-A DOCKER-USER -i {BRIDGE} -j {CHAIN}\n', '')
    code, out, _ = host.run()
    assert code == 1 and out.splitlines()[0] == f'kernel_vs_repo hook_absent:{BRIDGE}', out
    # An allowance the repository does not have.
    host = Host(tmp_path)
    final = f'-A {CHAIN} -j DROP'
    host.kernel[('iptables', 'raw')] = host.kernel[('iptables', 'raw')].replace(final, f'-A {CHAIN} -d 198.51.100.7 -j ACCEPT\n' + final)
    code, out, _ = host.run()
    assert code == 1 and out.splitlines()[0] == f'kernel_vs_repo extra:-A {CHAIN} -d 198.51.100.7 -j ACCEPT', out
    # An IPv6 gap, in the kernel and in the file.
    host = Host(tmp_path)
    host.kernel[('ip6tables', 'raw')] = host.kernel[('ip6tables', 'raw')].replace(f'-A PREROUTING -i {BRIDGE} -j DROP\n', '')
    code, out, _ = host.run()
    assert code == 1 and out.splitlines()[0] == 'kernel_vs_repo table_differs:raw', out  # the IPv6 raw table lost its only rule
    host = Host(tmp_path)
    host.files['v6'].write_text(host.files['v6'].read_text().replace(f'-A DOCKER-USER -i {BRIDGE} -j DROP\n', ''))
    code, out, _ = host.run()
    assert code == 1 and out.splitlines()[0] == f'file_vs_repo hook_absent:{BRIDGE}', out
    # A hook that is not the generated one (the inference-only text), and a hook that cannot run.
    host = Host(tmp_path)
    host.hook.write_text(run_script(INFERENCE, 'rules', '--mini-ip', MINI, '--format', 'after-init')[1])
    code, out, _ = host.run()
    assert code == 1 and out.splitlines()[0].startswith('file_vs_repo '), out
    host = Host(tmp_path)
    host.hook.chmod(0o644)
    code, out, _ = host.run()
    assert code == 1 and 'file_vs_repo' in out, out
    # The inference verify notices a hook that lost its recordings half, the delivery block is not part of either.
    host = Host(tmp_path)
    host.hook.write_text(run_script(INFERENCE, 'rules', '--mini-ip', MINI, '--format', 'after-init')[1])
    assert host.run(INFERENCE)[:2] == (1, 'file_vs_repo table_differs:raw\n')
    host.env.write_text(f'INFERENCE_MINI_IP={MINI}\n')
    assert host.run(INFERENCE)[:2] == (0, 'ok\n')  # without a recordings resolver the hook is the inference-only text


# spec: ANYM-712
def test_verify_needs_both_inputs_for_the_composed_hook(tmp_path):
    host = Host(tmp_path)
    host.env.write_text(f'RECORDINGS_RESOLVER={RESOLVER}\n')
    code, out, err = host.run()
    assert code == 2 and 'INFERENCE_MINI_IP' in err, (out, err)
    host.env.write_text(f'INFERENCE_MINI_IP={MINI}\n')
    code, out, err = host.run()
    assert code == 2 and 'RECORDINGS_RESOLVER' in err, (out, err)


# spec: ANYM-712
def test_the_boot_receipt_holds_the_recordings_pair_once_it_is_configured(tmp_path):
    out, state, env = tmp_path / 'firewall-verify.json', tmp_path / 'raw-step.json', tmp_path / 'env'
    state.write_text(json.dumps({'boot_id': 'b-1', 'uptime': 12.5, 'ok': True, 'reason': 'ok'}))
    base = [sys.executable, '-I', str(PERSIST), 'receipt', '--out', str(out), '--boot-id', 'b-1', '--raw-state', str(state),
            '--env-file', str(env), '--systemctl', 'true', '--docker', 'true', '--verify', 'inference=echo ok', '--verify', 'delivery=echo ok']
    env.write_text('INFERENCE_MINI_IP=1\n')
    done = subprocess.run(base, text=True, capture_output=True, timeout=30)
    receipt = json.loads(out.read_text())
    assert done.returncode == 0 and receipt['ok'] is True and receipt['recordings'] == 'not_installed'
    env.write_text(f'RECORDINGS_RESOLVER={RESOLVER}\n')
    done = subprocess.run([*base, '--verify', 'recordings=sh -c "echo kernel_vs_repo table_differs:raw; exit 1"'], text=True, capture_output=True, timeout=30)
    receipt = json.loads(out.read_text())
    assert done.returncode == 1 and receipt['ok'] is False and receipt['recordings'] == 'kernel_vs_repo table_differs:raw'
    done = subprocess.run([*base, '--verify', 'recordings=echo ok'], text=True, capture_output=True, timeout=30)
    receipt = json.loads(out.read_text())
    assert done.returncode == 0 and receipt['ok'] is True and receipt['recordings'] == 'ok'
    # Configured, and the real recordings verify cannot read this env file's host: never `ok`.
    done = subprocess.run(base, text=True, capture_output=True, timeout=30)
    assert done.returncode == 1 and json.loads(out.read_text())['recordings'] != 'ok'


# spec: ANYM-712
def test_the_receipt_passes_its_env_file_to_every_default_verifier(tmp_path, monkeypatch):
    import importlib.util
    module_spec = importlib.util.spec_from_file_location('firewall_persist_under_test', PERSIST)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    candidate = tmp_path / 'candidate.env'
    candidate.write_text(f'RECORDINGS_RESOLVER={RESOLVER}\nINFERENCE_MINI_IP={MINI}\n')
    seen = []

    def fake(command, **kwargs):
        seen.append(command)
        return subprocess.CompletedProcess(command, 0, stdout='ok\n', stderr='')
    monkeypatch.setattr(module.subprocess, 'run', fake)
    state = tmp_path / 'raw-step.json'
    state.write_text('{}')
    module.receipt_cli(['--out', str(tmp_path / 'receipt.json'), '--boot-id', 'b-1', '--raw-state', str(state),
                        '--env-file', str(candidate), '--systemctl', 'true', '--docker', 'true'])
    verifiers = [command for command in seen if 'verify' in command]
    assert [Path(command[1]).name for command in verifiers] == ['inference_egress_check.py', 'delivery_egress_check.py', 'recordings_egress_check.py']
    # Every verifier reads the file the receipt was asked about, never its own default.
    assert all(command[-2:] == ['--env-file', str(candidate)] for command in verifiers), verifiers


def namespace_ready():
    if shutil.which('sudo') is None or shutil.which('unshare') is None or shutil.which('iptables-restore') is None:
        return False
    return subprocess.run(['sudo', '-n', 'unshare', '-n', 'true'], capture_output=True).returncode == 0


needs_namespace = pytest.mark.skipif(not namespace_ready(), reason='needs sudo, unshare and iptables-restore on Linux')


def in_namespace(script, timeout=120):
    return subprocess.run(['sudo', '-n', 'unshare', '-n', 'sh', '-ec', script], text=True, capture_output=True, timeout=timeout)


NET_HELPER = """import socket, sys, time
if sys.argv[1] == 'serve':
    server = socket.create_server(('0.0.0.0', int(sys.argv[2])))
    server.settimeout(0.5)
    end = time.monotonic() + 40
    while time.monotonic() < end:
        try:
            conn = server.accept()[0]
        except OSError:
            continue
        conn.sendall(b'ok')
        conn.close()
else:
    try:
        with socket.create_connection((sys.argv[2], int(sys.argv[3])), timeout=2) as conn:
            sys.exit(0 if conn.recv(2) == b'ok' else 1)
    except OSError:
        sys.exit(1)
"""
TOPOLOGY = """
modprobe br_netfilter 2>/dev/null || true
[ -e /proc/sys/net/bridge/bridge-nf-call-iptables ] || exit 77
sysctl -qw net.bridge.bridge-nf-call-iptables=1 || exit 77
ip link add dk-recbackend0 type bridge && ip link set dk-recbackend0 up || exit 77
unshare -n sleep 90 >/dev/null 2>&1 &
GW=$!
unshare -n sleep 90 >/dev/null 2>&1 &
REC=$!
sleep 0.5
ip link add vgw type veth peer name vgw1 && ip link add vrec type veth peer name vrec1 || exit 77
ip link set vgw1 netns $GW && ip link set vrec1 netns $REC || exit 77
ip link set vgw master dk-recbackend0 && ip link set vgw up && ip link set vrec master dk-recbackend0 && ip link set vrec up || exit 77
nsenter -t $GW -n sh -c 'ip addr add 10.77.0.2/24 dev vgw1 && ip link set vgw1 up && ip link set lo up' || exit 77
nsenter -t $REC -n sh -c 'ip addr add 10.77.0.3/24 dev vrec1 && ip link set vrec1 up && ip link set lo up' || exit 77
# Docker's own rules: DOCKER-USER first, then the inter-container accept on the bridge.
iptables -N DOCKER-USER; iptables -P FORWARD DROP; iptables -A FORWARD -j DOCKER-USER
iptables -A FORWARD -i dk-recbackend0 -o dk-recbackend0 -j ACCEPT
nsenter -t $GW -n python3 HELPER serve 13240 >/dev/null 2>&1 &
nsenter -t $REC -n python3 HELPER serve 13234 >/dev/null 2>&1 &
sleep 1
gw_to_rec() { nsenter -t $GW -n python3 HELPER connect 10.77.0.3 13234; }
rec_to_gw() { nsenter -t $REC -n python3 HELPER connect 10.77.0.2 13240; }
# The baseline proves the topology works here (both directions open); otherwise this environment cannot run the test.
gw_to_rec && rec_to_gw || exit 77
sh APPLY
if gw_to_rec; then echo gateway_to_recordings=ok; else echo gateway_to_recordings=fail; fi
if rec_to_gw; then echo recordings_to_gateway=ok; else echo recordings_to_gateway=fail; fi
kill $GW $REC 2>/dev/null || true
kill $(jobs -p) 2>/dev/null || true
"""


# spec: ANYM-712
@needs_namespace
@pytest.mark.skipif(shutil.which('nsenter') is None or shutil.which('ip') is None, reason='needs nsenter and ip')
def test_on_a_real_bridge_the_gateway_reaches_recordings_and_gets_replies_but_recordings_cannot_open_a_connection_to_the_gateway(tmp_path):
    helper, apply = tmp_path / 'net.py', tmp_path / 'apply.sh'
    helper.write_text(NET_HELPER)
    # The filter half of the command form: the chain, its rules and the DOCKER-USER hooks (host-bound INPUT drops and the
    # raw table are not part of this path).
    lines = [line for line in commands() if line.startswith('iptables ') and ' -t raw ' not in line and ' -I INPUT ' not in line]
    apply.write_text('\n'.join(lines) + '\n')
    result = in_namespace(TOPOLOGY.replace('HELPER', str(helper)).replace('APPLY', str(apply)), timeout=180)
    if result.returncode == 77:
        pytest.skip('this environment cannot build the bridge topology (br_netfilter, veth or nsenter)')
    assert result.returncode == 0, result.stderr
    assert 'gateway_to_recordings=ok' in result.stdout and 'recordings_to_gateway=fail' in result.stdout, (result.stdout, result.stderr)


# spec: ANYM-712
@needs_namespace
def test_repeated_loads_add_each_jump_once_and_leave_docker_and_inference_raw_rules_alone(tmp_path):
    hook, state = tmp_path / 'after.init', tmp_path / 'raw-step.json'
    hook.write_text(hook_text())
    script = (f'iptables -t raw -A PREROUTING -d 192.0.2.77 -j ACCEPT; DEEDKIT_RAW_STATE={state} sh {hook} start; '
              f'DEEDKIT_RAW_STATE={state} sh {hook} start; DEEDKIT_RAW_STATE={state} sh {hook} start; '
              'iptables-save -t raw; ip6tables-save -t raw')
    result = in_namespace(script)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines.count(f'-A PREROUTING -i {BRIDGE} -j {CHAIN}') == 1 and lines.count(f'-A PREROUTING -i {BRIDGE} -j DROP') == 1
    assert lines.count('-A PREROUTING -i dk-inference0 -j DEEDKIT-INFERENCE') == 1 and lines.count('-A PREROUTING -d 192.0.2.77/32 -j ACCEPT') == 1
    own = [line for line in lines if line.startswith(f'-A {CHAIN} ')]
    assert len(own) == len(chain_of(commands(), 'raw')) and own[-1] == f'-A {CHAIN} -j DROP'
    assert f'-A {CHAIN} -p tcp -m tcp --dport 443 -j RETURN' in own
    assert sum(line.startswith('-A DEEDKIT-INFERENCE ') for line in lines) == len([x for x in inference_commands() if ' -t raw -A DEEDKIT-INFERENCE ' in x])
    record = json.loads(state.read_text())
    assert record['ok'] is True and record['reason'] == 'ok'


# spec: ANYM-712
@needs_namespace
def test_an_injected_failure_or_hang_keeps_the_previous_complete_recordings_chain_and_the_inference_half_still_loads(tmp_path):
    real = shutil.which('iptables-restore')
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    (first / 'after.init').write_text(hook_text())
    (second / 'after.init').write_text(hook_text(resolver='192.0.2.54', mini='100.101.102.104'))
    wrappers, state = tmp_path / 'bin', tmp_path / 'raw-step.json'
    # The restores are numbered by call: the first is the inference restore, the second the recordings restore (it runs last).
    counter = tmp_path / 'calls'
    head = f'#!/bin/sh\nn=$(cat {counter} 2>/dev/null || echo 0); echo $((n+1)) > {counter}\n'
    variants = {
        # The recordings restore commits nothing: its input is cut after three lines, no COMMIT.
        'recordings_error': head + f'case $n in 1) head -n 3 | {real} "$@"; exit 1;; esac\nexec {real} "$@"\n',
        # Every restore hangs: the inference half fails at its own bound and the recordings half is never started.
        'hang': '#!/bin/sh\nexec sleep 60\n',
        # The inference restore is slow (2 s) but succeeds, then the recordings restore hangs under the DEFAULT 30 s limit:
        # the recordings budget ends it and nothing it left behind holds the hook.
        'recordings_hang': head + f'case $n in 0) sleep 2;; 1) exec sleep 60;; esac\nexec {real} "$@"\n'}
    for name, body in variants.items():
        (wrappers / name).mkdir(parents=True)
        (wrappers / name / 'iptables-restore').write_text(body)
        (wrappers / name / 'iptables-restore').chmod(0o755)
    for name in variants:
        counter.unlink(missing_ok=True)
        limits = 'DEEDKIT_RECORDINGS_TIMEOUT=3' if name == 'recordings_hang' else 'DEEDKIT_STEP_TIMEOUT=2'
        script = (f'DEEDKIT_RAW_STATE={state} sh {first}/after.init start; iptables-save -t raw > {tmp_path}/{name}.before; '
                  f'PATH={wrappers}/{name}:$PATH {limits} DEEDKIT_RAW_STATE={state} sh {second}/after.init start; '
                  f'echo rc=$?; iptables-save -t raw > {tmp_path}/{name}.after')
        started = time.monotonic()
        result = in_namespace(script)
        # With a 3 s recordings budget (+2 s grace) and a 2 s inference restore the whole second run ends well inside 20 s.
        assert time.monotonic() - started < (20 if name == 'recordings_hang' else 40), name
        assert result.returncode == 0 and 'rc=0' in result.stdout, (name, result.stderr)
        before, after = (tmp_path / f'{name}.before').read_text(), (tmp_path / f'{name}.after').read_text()
        mine = lambda text: [re.sub(r'\[\d+:\d+\]', '', line) for line in text.splitlines() if CHAIN in line]
        # The previous complete recordings chain stays, never an empty or half-written one.
        assert mine(before) == mine(after) and f'-d {RESOLVER}/32' in after and '192.0.2.54' not in after, name
        record = json.loads(state.read_text())
        assert record['ok'] is False, (name, record)
        if name == 'recordings_hang':
            # The child's own command limit and the outer budget are the same value: either one ends the hang first.
            assert '100.101.102.104/32' in after and record['reason'] in ('hook_recordings_124', 'restore_recordings_v4'), (record, after)
            continue
        if name == 'recordings_error':
            # The inference half loaded first and stays; the recordings failure is the recorded reason.
            assert '100.101.102.104/32' in after and record['reason'] == 'restore_recordings_v4', (record, after)
        else:
            assert '100.101.102.104' not in after and record['reason'].startswith('restore'), (record, after)


# spec: ANYM-712
def test_the_hook_runs_the_recordings_half_last_so_it_can_never_delay_or_break_the_inference_half():
    text = hook_text()
    step = text[text.index('\nstep() {\n'):]
    # The inference restore and both of its hooks run exactly as before, then the bounded recordings child, then the record.
    order = [step.index(marker) for marker in ('iptables-restore', 'ensure iptables', 'ensure ip6tables', 'sh "$0" recordings', 'record true ok')]
    assert order == sorted(order)
    assert 'REC_FAIL=restore_recordings_v4' in text and 'REC_FAIL=hook_recordings_v4' in text and 'REC_FAIL=hook_recordings_v6' in text
    # Nothing of the recordings half is before the inference commands inside step().
    assert 'recordings' not in step[:step.index('ensure ip6tables')]


def listener():
    server = socket.create_server(('127.0.0.1', 0), backlog=8)
    stop = threading.Event()

    def accept():
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                server.accept()[0].close()
            except OSError:
                pass
    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    return server, stop, thread


def dns_responder():
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(('127.0.0.1', 0))
    udp.settimeout(0.2)
    stop = threading.Event()

    def answer():
        while not stop.is_set():
            try:
                data, peer = udp.recvfrom(512)
                udp.sendto(data[:2] + b'\x81\x80', peer)
            except OSError:
                pass
    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    return udp, stop, thread


def silent_port():
    """A local TCP port nothing answers on: a refusal (closed port), used as the negative the check must not accept."""
    probe = socket.socket()
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


# spec: ANYM-712
def test_check_passes_open_targets_and_blocked_ones_only_when_nothing_answers_never_on_a_refusal():
    server, stop, thread = listener()
    udp, dns_stop, dns_thread = dns_responder()
    try:
        open_port, dns_port = server.getsockname()[1], udp.getsockname()[1]
        code, out, err = run('check', '--open', f'127.0.0.1:{open_port}', '--open-dns', f'127.0.0.1:{dns_port}', '--control', f'127.0.0.1:{open_port}')
        report = json.loads(out)
        assert code == 0 and report['passed'] and [r['outcome'] for r in report['results']] == ['connected', 'answered', 'connected'], (out, err)
        # A blocked target that connects is a failure; one that refuses is a failure too (a DROP never sends a refusal).
        code, out, _ = run('check', '--blocked', f'127.0.0.1:{open_port}')
        assert code == 1 and json.loads(out)['results'][0]['outcome'] == 'connected'
        code, out, _ = run('check', '--blocked', f'127.0.0.1:{silent_port()}')
        assert code == 1 and json.loads(out)['results'][0]['outcome'] == 'refused'
        code, out, _ = run('check', '--blocked-dns', f'127.0.0.1:{dns_port}')
        assert code == 1 and json.loads(out)['results'][0]['outcome'] == 'answered'
        # An unresolved or malformed target and an empty check fail too; a name is never accepted as a blocked target.
        code, out, _ = run('check', '--blocked', 'not-an-address:443')
        assert code == 1 and json.loads(out)['results'][0]['outcome'] == 'malformed'
        assert run('check')[0] == 1
        # No route / no answer is what a blocked target must look like: 192.0.2.1 (TEST-NET-1) has no listener anywhere.
        code, out, _ = run('check', '--blocked', '192.0.2.1:9', '--timeout', '0.5')
        assert json.loads(out)['results'][0]['outcome'] in ('timeout', 'unreachable') and code == 0
    finally:
        stop.set()
        dns_stop.set()
        thread.join(2)
        dns_thread.join(2)
        server.close()
        udp.close()


# spec: ANYM-712
def test_check_runs_from_stdin_with_no_import_of_the_repository_and_listen_serves_a_bounded_control():
    source = SCRIPT.read_text()
    server, stop, thread = listener()
    try:
        port = server.getsockname()[1]
        code, out, err = run_script('-', 'check', '--open', f'127.0.0.1:{port}', stdin=source)
        assert code == 0 and json.loads(out)['passed'], err
    finally:
        stop.set()
        thread.join(2)
        server.close()
    done = subprocess.run([sys.executable, '-I', str(SCRIPT), 'listen', '--port', '0', '--seconds', '0.3'], text=True, capture_output=True, timeout=30)
    assert done.returncode == 0 and json.loads(done.stdout.splitlines()[0])['listening'] > 0


OVERLAY = ROOT / 'deploy/compose.recordings.yaml'


# spec: ANYM-712
def test_only_recordings_api_joins_the_egress_bridge_with_the_scripts_name_and_no_third_network():
    document = yaml.safe_load(OVERLAY.read_text())
    script_bridge = re.search(r"^BRIDGE = '([^']+)'", SCRIPT.read_text(), re.M).group(1)
    egress = document['networks']['recordings-egress']
    assert egress['driver_opts']['com.docker.network.bridge.name'] == script_bridge == BRIDGE and egress['enable_ipv6'] is False
    joined = [name for name, unit in document['services'].items() if 'recordings-egress' in unit.get('networks', {})]
    assert joined == ['recordings-api']
    networks = document['services']['recordings-api']['networks']
    assert set(networks) == {'recordings-backend', 'recordings-egress'} and 'integration' not in networks
    assert 'integration' not in document['networks'] and document['networks']['recordings-backend']['external'] is True
    # The admission is on in production and both probe lists are required (compose refuses a missing one).
    # Docker's embedded resolver queries the explicit servers from the container's namespace, where the bridge rules apply;
    # without `dns` it would use the host's resolvers from the host namespace, outside them (as in the delivery overlay).
    dns = document['services']['recordings-api']['dns']
    assert len(dns) == 1 and dns[0].startswith('${RECORDINGS_RESOLVER:?')
    environment = document['services']['recordings-api']['environment']
    assert environment['RECORDINGS_EGRESS_ADMISSION'] == 'required'
    assert environment['RECORDINGS_EGRESS_BLOCKED'].startswith('${RECORDINGS_EGRESS_BLOCKED:?')
    assert environment['RECORDINGS_EGRESS_OPEN'].startswith('${RECORDINGS_EGRESS_OPEN:?')
    for other in ROOT.glob('deploy/compose*.yaml'):
        if other != OVERLAY:
            assert 'recordings-egress' not in other.read_text() and 'dk-recordings0' not in other.read_text(), other.name
