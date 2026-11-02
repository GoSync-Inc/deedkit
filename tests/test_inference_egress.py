"""The mini-facing enrichment worker's host rules and egress check (ANYM-688); local sockets only, no firewall is changed."""
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

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/inference_egress_check.py'
MINI = '100.101.102.103'
CHAIN = 'DEEDKIT-INFERENCE'


def run(*args):
    result = subprocess.run([sys.executable, '-I', str(SCRIPT), *args], text=True, capture_output=True, timeout=30)
    return result.returncode, result.stdout, result.stderr


def rules(*extra):
    code, out, err = run('rules', '--mini-ip', MINI, *extra)
    assert code == 0, err
    return out.splitlines()


def chain_rules(lines):
    return [line for line in lines if line.startswith(f'iptables -A {CHAIN} ')]


# spec: ANYM-688
def test_egress_rules_allow_exactly_the_mini_on_443_and_drop_everything_else():
    lines = rules()
    chain = chain_rules(lines)
    # The one allow comes first; everything after it is a drop, ending in a catch-all.
    assert chain[0] == f'iptables -A {CHAIN} -d {MINI} -p tcp --dport 443 -j RETURN'
    assert all(line.endswith('-j DROP') for line in chain[1:]) and chain[-1] == f'iptables -A {CHAIN} -j DROP'
    assert not any('ACCEPT' in line for line in lines)
    # The ranges the card names are dropped explicitly: RFC 1918, link-local with metadata, the tailnet, loopback.
    for network in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '169.254.0.0/16', '100.64.0.0/10', '127.0.0.0/8'):
        assert f'iptables -A {CHAIN} -d {network} -j DROP' in chain, network
    # Only the egress bridge is sent through the chain; host-bound packets and all IPv6 are dropped.
    assert f'iptables -I DOCKER-USER -i dk-inference0 -j {CHAIN}' in lines
    assert 'iptables -I INPUT -i dk-inference0 -j DROP' in lines
    assert 'ip6tables -I DOCKER-USER -i dk-inference0 -j DROP' in lines
    assert 'ip6tables -I INPUT -i dk-inference0 -j DROP' in lines
    # The chain exists before it is filled and is emptied first, so a second run never doubles the rules.
    assert lines.index(f'iptables -N {CHAIN}') < lines.index(f'iptables -F {CHAIN}') < lines.index(chain[0])
    # The same chain also hangs in `raw` PREROUTING, which runs before the filter FORWARD chain: a Tailscale
    # `ts-forward` accept placed ahead of DOCKER-USER (or re-placed after a tailscaled restart) cannot pass it.
    raw = [line.replace('iptables -t raw ', 'iptables ') for line in lines if line.startswith('iptables -t raw -A ')]
    assert raw == chain
    assert f'iptables -t raw -I PREROUTING -i dk-inference0 -j {CHAIN}' in lines
    assert 'ip6tables -t raw -I PREROUTING -i dk-inference0 -j DROP' in lines
    assert lines.index(f'iptables -t raw -N {CHAIN}') < lines.index(f'iptables -t raw -I PREROUTING -i dk-inference0 -j {CHAIN}')
    assert sum(' -j RETURN' in line for line in lines) == 2


# spec: ANYM-688
def test_egress_rules_drop_dns_and_the_db_bridge_host_packets():
    lines = rules()
    chain = chain_rules(lines)
    # DNS to any address (the mini included: its port 53 is not 443) is dropped, after the one allow.
    for protocol in ('udp', 'tcp'):
        drop = f'iptables -A {CHAIN} -p {protocol} --dport 53 -j DROP'
        assert drop in chain and chain.index(drop) > 0
    # The isolated db bridge has no host address; its host-bound drop is the second layer. It is never forwarded
    # through the egress chain, and it has no IPv6.
    assert 'iptables -I INPUT -i dk-inferencedb0 -j DROP' in lines
    assert 'ip6tables -I INPUT -i dk-inferencedb0 -j DROP' in lines
    assert not any('DOCKER-USER' in line and 'dk-inferencedb0' in line for line in lines)


# spec: ANYM-688
def test_egress_rules_undo_is_the_mirror():
    applied, undone = rules(), run('rules', '--undo')[1].splitlines()
    inserts = {line.replace(' -I ', ' -D ', 1) for line in applied if ' -I ' in line}
    deletes = {line for line in undone if ' -D ' in line}
    assert inserts == deletes and inserts
    # The hooks are removed before either chain is flushed and deleted, or the delete would fail.
    assert undone[-4:] == [f'iptables -F {CHAIN}', f'iptables -X {CHAIN}',
                           f'iptables -t raw -F {CHAIN}', f'iptables -t raw -X {CHAIN}']
    assert max(undone.index(line) for line in deletes) < undone.index(f'iptables -F {CHAIN}')
    # `--undo` needs no address: it removes what any address added. A given address changes nothing.
    assert rules('--undo') == undone


# spec: ANYM-688
def test_egress_rules_refuse_a_non_tailnet_or_ipv6_address():
    for bad in ('192.168.1.5', '10.0.0.1', '8.8.8.8', '100.63.255.255', '100.128.0.0', '127.0.0.1', '169.254.169.254',
                'fd7a:115c:a1e0::1', '::1', 'deedkit-inference', '100.101.102.103/32', ''):
        code, out, err = run('rules', '--mini-ip', bad)
        assert code != 0 and out == '' and err, bad
    code, out, err = run('rules')
    assert code != 0 and out == ''
    for good in ('100.64.0.1', '100.127.255.254', MINI):
        code, out, _ = run('rules', '--mini-ip', good)
        assert code == 0 and f'-d {good} -p tcp --dport 443 -j RETURN' in out


PERSIST = Path(__file__).resolve().parents[1] / 'scripts/firewall_persist.py'
UNIT = Path(__file__).resolve().parents[1] / 'deploy/deedkit-firewall-verify.service'
# A minimal ufw before.rules / before6.rules with the delivery block already in place (what production holds).
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


def namespace_ready():
    if shutil.which('sudo') is None or shutil.which('unshare') is None or shutil.which('iptables-restore') is None:
        return False
    return subprocess.run(['sudo', '-n', 'unshare', '-n', 'true'], capture_output=True).returncode == 0


needs_namespace = pytest.mark.skipif(not namespace_ready(), reason='needs sudo, unshare and iptables-restore on Linux')


def in_namespace(script, timeout=120):
    return subprocess.run(['sudo', '-n', 'unshare', '-n', 'sh', '-ec', script], text=True, capture_output=True,
                          timeout=timeout)


def ufw_block(family, *extra):
    code, out, err = run('rules', '--mini-ip', MINI, '--format', 'ufw', '--family', family, *extra)
    assert code == 0, err
    return out


def ufw_files(tmp_path):
    """before.rules and before6.rules as ufw holds them, with the inference block merged in; the delivery-only pair."""
    files = {}
    for family, template in (('v4', TEMPLATE_V4), ('v6', TEMPLATE_V6)):
        base = tmp_path / f'base.{family}'
        base.write_text(template)
        full = tmp_path / f'full.{family}'
        full.write_text(ufw_block(family, '--source', str(base)))
        files[family], files[f'base_{family}'] = full, base
    return files


def after_init(tmp_path):
    code, out, err = run('rules', '--mini-ip', MINI, '--format', 'after-init')
    assert code == 0, err
    path = tmp_path / 'after.init'
    path.write_text(out)
    path.chmod(0o755)
    return path


def command_lines(mini=MINI):
    return [line for line in rules() if ' -N ' not in line and ' -F ' not in line]


def save_text(commands, binary, table, extra=(), drop=()):
    """What `iptables-save -t TABLE` prints for the command form (nft style: -m tcp, /32), plus unrelated rules."""
    body = [f':{CHAIN} - [0:0]'] if binary == 'iptables' else []
    for command in commands:
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
    return '\n'.join([f'*{table}', *body, *extra, *unrelated[table], 'COMMIT', ''])


class Host:
    """A fixture host: ufw files, the after.init hook, the env file and an iptables-save pair that cat fixtures."""

    def __init__(self, tmp_path, mini=MINI):
        self.tmp, self.mini = tmp_path, mini
        self.files = ufw_files(tmp_path)
        self.hook = after_init(tmp_path)
        self.env = tmp_path / 'env'
        self.env.write_text(f'INFERENCE_MINI_IP={MINI}\n')
        self.receipt = tmp_path / 'firewall-verify.json'
        commands = command_lines()
        self.kernel = {(b, t): save_text(commands, b, t) for b in ('iptables', 'ip6tables') for t in ('filter', 'raw')}

    def run(self, *extra, boot='boot-1'):
        for binary in ('iptables', 'ip6tables'):
            script = self.tmp / f'{binary}-save'
            texts = {t: self.kernel[(binary, t)] for t in ('filter', 'raw')}
            for table, text in texts.items():
                (self.tmp / f'{binary}.{table}').write_text(text)
            script.write_text(f'#!/bin/sh\ncat "{self.tmp}/{binary}.$2"\n')
            script.chmod(0o755)
        return run('verify', '--env-file', str(self.env), '--rules-v4', str(self.files['v4']),
                   '--rules-v6', str(self.files['v6']), '--after-init', str(self.hook),
                   '--iptables-save', str(self.tmp / 'iptables-save'), '--ip6tables-save', str(self.tmp / 'ip6tables-save'),
                   '--receipt', str(self.receipt), '--boot-id', boot, *extra)


# spec: ANYM-693
def test_ufw_form_is_exactly_the_filter_and_ipv6_rules_of_the_command_form_in_order():
    lines = rules()

    def ufw(line, chain):
        return ' '.join(['-A', *line.split()[2:]]).replace('-A INPUT ', f'-A {chain} ', 1) if ' -t raw ' not in line else None
    v4 = [ufw(line, 'ufw-before-input') for line in lines if line.startswith('iptables -') and ' -N ' not in line and ' -F ' not in line]
    v6 = [ufw(line, 'ufw6-before-input') for line in lines if line.startswith('ip6tables -')]
    block4, block6 = ufw_block('v4').splitlines(), ufw_block('v6').splitlines()
    assert block4[0] == '# deedkit-inference begin' and block4[-1] == '# deedkit-inference end'
    assert block4[1] == f':{CHAIN} - [0:0]' and 'DOCKER-USER - [0:0]' not in ufw_block('v4')
    assert block4[2:-1] == [rule for rule in v4 if rule]
    assert block6 == ['# deedkit-inference begin', *[rule for rule in v6 if rule], '# deedkit-inference end']
    # The filter block never holds a raw rule; the raw table is the after.init step's.
    assert 'PREROUTING' not in ufw_block('v4') + ufw_block('v6')
    # The mini's address appears once in the filter chain and nowhere else.
    assert ufw_block('v4').count(MINI) == 1 and MINI not in ufw_block('v6')


# spec: ANYM-693
def test_ufw_form_refuses_an_address_outside_the_tailnet_and_needs_a_family():
    for bad in ('192.168.1.5', '8.8.8.8', 'fd7a:115c:a1e0::1', ''):
        code, out, err = run('rules', '--mini-ip', bad, '--format', 'ufw', '--family', 'v4')
        assert code != 0 and out == '' and err, bad
    assert run('rules', '--mini-ip', MINI, '--format', 'ufw')[0] != 0


# spec: ANYM-693
def test_merge_puts_the_block_after_the_delivery_block_replaces_its_own_and_refuses_an_unsafe_file(tmp_path):
    base = tmp_path / 'before.rules'
    base.write_text(TEMPLATE_V4)
    merged = ufw_block('v4', '--source', str(base))
    lines = merged.splitlines()
    begin = lines.index('# deedkit-inference begin')
    # After the delivery hook and input drop (so `:DOCKER-USER` is declared before it), before the file's own later rules.
    assert lines.index('-A DOCKER-USER -i dk-delivery0 -j DEEDKIT-DELIVERY') < begin < lines.index('-A ufw-before-input -i lo -j ACCEPT')
    assert merged.count('# deedkit-inference begin') == 1 and merged.endswith('COMMIT\n')
    # Merging again, with another address, replaces the block rather than adding a second one.
    base.write_text(merged)
    again = run('rules', '--mini-ip', '100.101.102.104', '--format', 'ufw', '--family', 'v4', '--source', str(base))[1]
    assert again.count('# deedkit-inference begin') == 1 and '100.101.102.104' in again and MINI not in again
    # No delivery block to follow, or no `:DOCKER-USER` declared before the insertion point: refused, nothing printed.
    bare = tmp_path / 'bare.rules'
    bare.write_text('*filter\n:ufw-before-input - [0:0]\n# End required lines\nCOMMIT\n')
    code, out, err = run('rules', '--mini-ip', MINI, '--format', 'ufw', '--family', 'v4', '--source', str(bare))
    assert code != 0 and out == '' and ('DOCKER-USER' in err or 'delivery' in err)


# spec: ANYM-693
def test_ufw_form_passes_iptables_restore_test_for_syntax(tmp_path):
    if shutil.which('iptables-restore') is None:
        pytest.skip('iptables-restore is not installed here')
    files = ufw_files(tmp_path)
    for binary, family in (('iptables-restore', 'v4'), ('ip6tables-restore', 'v6')):
        result = subprocess.run(['sudo', '-n', binary, '--test', '--noflush', str(files[family])], text=True, capture_output=True)
        if 'Permission denied' in result.stderr or 'must be root' in result.stderr or 'sudo' in result.stderr:
            pytest.skip('the runner refuses iptables-restore --test: ' + result.stderr.strip()[:80])
        assert result.returncode == 0, result.stderr


# spec: ANYM-693
@needs_namespace
def test_ufw_block_restores_once_in_a_disposable_namespace(tmp_path):
    files = ufw_files(tmp_path)
    base = f'iptables-restore --noflush < {files["base_v4"]}; ip6tables-restore --noflush < {files["base_v6"]}; '
    twice = ''.join(f'iptables-restore --noflush < {files["v4"]}; ip6tables-restore --noflush < {files["v6"]}; ' for _ in range(2))
    results = {}
    for start in ('empty', 'delivery'):
        directory = tmp_path / start
        directory.mkdir()
        save = lambda name: f'iptables-save > {directory}/{name}4; ip6tables-save > {directory}/{name}6; '
        result = in_namespace((base + save('before') if start == 'delivery' else '') + twice + save('after'))
        assert result.returncode == 0, result.stderr
        results[start] = directory
    expected = rules()
    for start, directory in results.items():
        after4, after6 = (directory / 'after4').read_text().splitlines(), (directory / 'after6').read_text().splitlines()
        # Each rule and each hook exactly once.
        assert after4.count('-A DOCKER-USER -i dk-inference0 -j DEEDKIT-INFERENCE') == 1, start
        assert after4.count('-A DOCKER-USER -i dk-delivery0 -j DEEDKIT-DELIVERY') == 1, start
        assert [line for line in after4 if line.startswith(f'-A {CHAIN} ')].count(
            f'-A {CHAIN} -d {MINI}/32 -p tcp -m tcp --dport 443 -j RETURN') == 1
        assert sum(line.startswith(f'-A {CHAIN} ') for line in after4) == len(chain_rules(expected))
        assert after4.count('-A ufw-before-input -i dk-inference0 -j DROP') == 1
        assert after4.count('-A ufw-before-input -i dk-inferencedb0 -j DROP') == 1
        assert after6.count('-A DOCKER-USER -i dk-inference0 -j DROP') == 1
        assert after6.count('-A ufw6-before-input -i dk-inference0 -j DROP') == 1
        assert after6.count('-A ufw6-before-input -i dk-inferencedb0 -j DROP') == 1
    # The delivery block is untouched by the inference block.
    before4, after4 = (results['delivery'] / 'before4').read_text().splitlines(), (results['delivery'] / 'after4').read_text().splitlines()
    delivery = lambda text: [line for line in text if 'DEEDKIT-DELIVERY' in line or 'dk-delivery' in line]
    assert delivery(before4) == delivery(after4) and delivery(before4)


# spec: ANYM-693
@needs_namespace
def test_raw_step_run_twice_leaves_each_raw_rule_once_and_other_raw_rules_alone(tmp_path):
    hook, state = after_init(tmp_path), tmp_path / 'raw-step.json'
    script = (f'iptables -t raw -A PREROUTING -d 192.0.2.77 -j ACCEPT; DEEDKIT_RAW_STATE={state} sh {hook} start; '
              f'DEEDKIT_RAW_STATE={state} sh {hook} start; iptables-save -t raw; ip6tables-save -t raw')
    result = in_namespace(script)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    expected = [line for line in save_text(command_lines(), 'iptables', 'raw').splitlines() if line.startswith(f'-A {CHAIN}')]
    assert [line for line in lines if line.startswith(f'-A {CHAIN} ')] == expected
    assert lines.count('-A PREROUTING -i dk-inference0 -j DEEDKIT-INFERENCE') == 1
    assert lines.count('-A PREROUTING -i dk-inference0 -j DROP') == 1
    assert lines.count('-A PREROUTING -d 192.0.2.77/32 -j ACCEPT') == 1
    record = json.loads(state.read_text())
    assert record['ok'] is True and record['uptime'] > 0 and record['boot_id']


# spec: ANYM-693
@needs_namespace
def test_raw_step_failure_or_hang_exits_zero_and_leaves_the_previous_complete_chain(tmp_path):
    real = shutil.which('iptables-restore')
    first = tmp_path / 'first'
    first.mkdir()
    (first / 'after.init').write_text(run('rules', '--mini-ip', MINI, '--format', 'after-init')[1])
    second = tmp_path / 'second'
    second.mkdir()
    (second / 'after.init').write_text(run('rules', '--mini-ip', '100.101.102.104', '--format', 'after-init')[1])
    wrappers = tmp_path / 'bin'
    wrappers.mkdir()
    state = tmp_path / 'raw-step.json'
    # The injection: a failing iptables-restore that commits nothing (an input cut after the first rule, no COMMIT).
    for name, body in (('error', f'#!/bin/sh\nhead -n 3 | {real} "$@"\nexit 1\n'), ('hang', '#!/bin/sh\nexec sleep 60\n')):
        folder = wrappers / name
        folder.mkdir()
        (folder / 'iptables-restore').write_text(body)
        (folder / 'iptables-restore').chmod(0o755)
    for name in ('error', 'hang'):
        script = (f'iptables -t raw -A PREROUTING -d 192.0.2.77 -j ACCEPT; DEEDKIT_RAW_STATE={state} sh {first}/after.init start; '
                  'iptables-save -t raw > ' + f'{tmp_path}/{name}.before; '
                  f'PATH={wrappers}/{name}:$PATH DEEDKIT_STEP_TIMEOUT=2 DEEDKIT_RAW_STATE={state} sh {second}/after.init start; '
                  f'echo rc=$?; iptables-save -t raw > {tmp_path}/{name}.after')
        started = time.monotonic()
        result = in_namespace(script)
        assert result.returncode == 0 and 'rc=0' in result.stdout, (name, result.stderr)
        assert time.monotonic() - started < 40
        before, after = (tmp_path / f'{name}.before').read_text(), (tmp_path / f'{name}.after').read_text()
        stable = lambda text: [re.sub(r'\[\d+:\d+\]', '', line) for line in text.splitlines() if not line.startswith('#')]
        assert stable(before) == stable(after), name
        assert f'-A {CHAIN} -d {MINI}/32 -p tcp -m tcp --dport 443 -j RETURN' in after and '100.101.102.104' not in after
        assert f'-A {CHAIN} -j DROP' in after and '-A PREROUTING -d 192.0.2.77/32 -j ACCEPT' in after
        record = json.loads(state.read_text())
        assert record['ok'] is False and record['reason'].startswith('restore'), record
        assert 'restore' in result.stderr


# spec: ANYM-693
def test_after_init_text_is_bounded_idempotent_and_never_stops_ufw():
    code, text, err = run('rules', '--mini-ip', MINI, '--format', 'after-init')
    assert code == 0 and text.startswith('#!/bin/sh\n'), err
    # One atomic restore of the raw chain, the jump only after it, a timeout on every firewall command, exit 0 always.
    assert text.count('iptables-restore --noflush -T raw') == 1 and text.count('DEEDKIT_V4') == 2
    assert text.index('iptables-restore --noflush -T raw') < text.index('ensure iptables')
    assert 'iptables -t raw -F' not in text and ' -X ' not in text
    assert text.rstrip().endswith('exit 0') and '/proc/uptime' in text
    assert all(line.lstrip().startswith(('timeout', '#', 'log', 'echo', 'record', 'fail', 'ensure', '[')) or 'timeout "$LIMIT"' in line
               for line in text.splitlines() if re.search(r'\biptables(-restore)?\b', line) and not line.startswith(('#', '-', ':')))
    assert 'LIMIT=30' in text
    # The generated text carries exactly the raw rules of the command form.
    raw = [line for line in text.splitlines() if line.startswith(f'-A {CHAIN} ')]
    assert raw == [line for line in ufw_block('v4').splitlines() if line.startswith(f'-A {CHAIN} ')]


# spec: ANYM-693
def test_verify_is_ok_when_repository_file_and_kernel_agree(tmp_path):
    code, out, err = Host(tmp_path).run()
    assert (code, out) == (0, 'ok\n'), (out, err)
    # ufw's own chain name for the host-bound drop and the command form's INPUT are the same fact.
    host = Host(tmp_path)
    host.kernel[('iptables', 'filter')] = host.kernel[('iptables', 'filter')].replace('-A INPUT -i dk-inference', '-A ufw-before-input -i dk-inference')
    assert host.run()[:2] == (0, 'ok\n')


# spec: ANYM-693
def test_verify_names_a_dropped_hook_an_extra_accept_and_a_changed_mini_address(tmp_path):
    host = Host(tmp_path)
    hook = f'-A DOCKER-USER -i dk-inference0 -j {CHAIN}'
    host.kernel[('iptables', 'filter')] = host.kernel[('iptables', 'filter')].replace(hook + '\n', '')
    code, out, _ = host.run()
    assert code == 1 and out.splitlines()[0] == 'kernel_vs_repo hook_absent:dk-inference0', out
    host = Host(tmp_path)
    final = f'-A {CHAIN} -j DROP'
    host.kernel[('iptables', 'filter')] = host.kernel[('iptables', 'filter')].replace(
        final, f'-A {CHAIN} -d 198.51.100.7/32 -j ACCEPT\n' + final)
    code, out, _ = host.run()
    assert code == 1 and out.splitlines()[0] == f'kernel_vs_repo extra:-A {CHAIN} -d 198.51.100.7 -j ACCEPT', out
    host = Host(tmp_path)
    host.env.write_text('INFERENCE_MINI_IP=100.101.102.104\n')
    code, out, _ = host.run()
    assert code == 1 and out.splitlines() == ['file_vs_repo mini_differs', 'kernel_vs_repo mini_differs'], out
    assert 'file_vs_kernel' not in out


# spec: ANYM-693
def test_verify_does_not_pass_an_allowance_that_file_and_kernel_share(tmp_path):
    host = Host(tmp_path)
    final = f'-A {CHAIN} -j DROP'
    extra = f'-A {CHAIN} -d 198.51.100.7 -j ACCEPT'
    host.kernel[('iptables', 'filter')] = host.kernel[('iptables', 'filter')].replace(final, extra + '\n' + final)
    host.files['v4'].write_text(host.files['v4'].read_text().replace(final, extra + '\n' + final))
    code, out, _ = host.run()
    # The file and the kernel agree with each other, and still the repository says no.
    assert code == 1 and out.splitlines() == [f'file_vs_repo extra:{extra}', f'kernel_vs_repo extra:{extra}'], out


# spec: ANYM-693
def test_verify_names_the_raw_table_when_only_the_filter_part_was_loaded(tmp_path):
    host = Host(tmp_path)
    host.kernel[('iptables', 'raw')] = save_text([], 'iptables', 'raw').replace(f':{CHAIN} - [0:0]\n', '')
    code, out, _ = host.run()
    assert code == 1 and out.splitlines() == ['kernel_vs_repo table_differs:raw', 'file_vs_kernel table_differs:raw'], out
    # A hook script that is not the generated one, or not executable (ufw would not run it), is the raw table's file side.
    host = Host(tmp_path)
    host.hook.write_text(host.hook.read_text() + '# edited\n')
    assert host.run()[1].splitlines() == ['file_vs_repo table_differs:raw']
    host = Host(tmp_path)
    host.hook.chmod(0o644)
    assert host.run()[1].splitlines()[0] == 'file_vs_repo table_differs:raw'


# spec: ANYM-693
def test_verify_persisted_only_reads_no_kernel_and_a_file_without_the_docker_user_declaration_fails(tmp_path):
    host = Host(tmp_path)
    code, out, err = host.run('--persisted-only', '--iptables-save', '/bin/false', '--ip6tables-save', '/bin/false')
    assert (code, out) == (0, 'ok\n'), err
    host.files['v4'].write_text(host.files['v4'].read_text().replace(':DOCKER-USER - [0:0]\n', ''))
    code, out, _ = host.run('--persisted-only')
    assert code == 1 and out.splitlines()[0] == 'file_vs_repo missing::DOCKER-USER', out
    # The kernel read failing in the full form is an error (exit 2), never a silent ok.
    assert Host(tmp_path).run('--iptables-save', '/bin/false')[0] == 2


# spec: ANYM-693
def test_verify_reads_a_boot_with_no_receipt_as_unknown_never_as_ok(tmp_path):
    host = Host(tmp_path)
    code, out, _ = host.run('--receipt-check')
    assert code == 1 and out.splitlines() == ['ok', 'receipt unknown'], out
    host.receipt.write_text(json.dumps({'boot_id': 'older-boot', 'ok': True}))
    assert host.run('--receipt-check')[1].splitlines() == ['ok', 'receipt unknown']
    host.receipt.write_text(json.dumps({'boot_id': 'boot-1', 'ok': False}))
    assert host.run('--receipt-check')[1].splitlines() == ['ok', 'receipt not_ok']
    host.receipt.write_text(json.dumps({'boot_id': 'boot-1', 'ok': True}))
    assert host.run('--receipt-check')[:2] == (0, 'ok\nreceipt ok\n')


def load_persist():
    import importlib.util
    spec = importlib.util.spec_from_file_location('firewall_persist', PERSIST)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# spec: ANYM-693
def test_receipt_order_is_yes_no_or_unknown():
    order = load_persist().rules_before_first_container
    assert order(ufw=10.0, raw=9.5, raw_ok=True, container=20.0) == 'yes'
    assert order(ufw=25.0, raw=9.5, raw_ok=True, container=20.0) == 'no'
    assert order(ufw=10.0, raw=21.0, raw_ok=True, container=20.0) == 'no'
    # A missing time or a failed step is unknown, never yes.
    assert order(ufw=None, raw=9.5, raw_ok=True, container=20.0) == 'unknown'
    assert order(ufw=10.0, raw=None, raw_ok=True, container=20.0) == 'unknown'
    assert order(ufw=10.0, raw=9.5, raw_ok=True, container=None) == 'unknown'
    assert order(ufw=10.0, raw=9.5, raw_ok=False, container=20.0) == 'unknown'


# spec: ANYM-693
def test_receipt_command_writes_the_boot_receipt_and_fails_when_a_verify_fails(tmp_path):
    out = tmp_path / 'firewall-verify.json'
    state = tmp_path / 'raw-step.json'
    state.write_text(json.dumps({'boot_id': 'b-1', 'uptime': 12.5, 'ok': True, 'reason': 'ok'}))
    base = [sys.executable, '-I', str(PERSIST), 'receipt', '--out', str(out), '--boot-id', 'b-1', '--raw-state', str(state),
            '--systemctl', 'true', '--docker', 'true']
    good = subprocess.run([*base, '--verify', 'inference=echo ok', '--verify', 'delivery=echo ok'], text=True, capture_output=True, timeout=30)
    receipt = json.loads(out.read_text())
    assert good.returncode == 0 and receipt['ok'] is True and receipt['boot_id'] == 'b-1'
    assert receipt['inference'] == 'ok' and receipt['delivery'] == 'ok' and receipt['raw_step_monotonic'] == 12.5
    assert receipt['rules_before_first_container'] == 'unknown' and json.loads(good.stdout) == receipt
    bad = subprocess.run([*base, '--verify', 'inference=sh -c "echo kernel_vs_repo table_differs:raw; exit 1"',
                          '--verify', 'delivery=echo ok'], text=True, capture_output=True, timeout=30)
    receipt = json.loads(out.read_text())
    assert bad.returncode == 1 and receipt['ok'] is False and receipt['inference'] == 'kernel_vs_repo table_differs:raw'
    # The raw step's record of another boot is not this boot's time.
    state.write_text(json.dumps({'boot_id': 'b-0', 'uptime': 12.5, 'ok': True, 'reason': 'ok'}))
    subprocess.run([*base, '--verify', 'inference=echo ok', '--verify', 'delivery=echo ok'], text=True, capture_output=True, timeout=30)
    assert json.loads(out.read_text())['raw_step_monotonic'] is None


# spec: ANYM-693
def test_verifier_unit_is_bounded_and_nothing_waits_for_it():
    text = UNIT.read_text()
    sections = {}
    current = None
    for line in text.splitlines():
        if line.startswith('['):
            current = line.strip('[]')
        elif '=' in line and not line.startswith('#'):
            key, _, value = line.partition('=')
            sections.setdefault(current, {}).setdefault(key, []).append(value)
    unit, service, install = sections['Unit'], sections['Service'], sections.get('Install', {})
    assert service['Type'] == ['oneshot'] and int(service['TimeoutStartSec'][0].rstrip('s')) <= 60
    assert 'ufw.service' in unit['After'][0] and 'docker.service' in unit['After'][0]
    # Nothing orders after it, nothing requires it, and no target waits for it.
    for key in ('Before', 'Requires', 'RequiredBy', 'BindsTo'):
        assert key not in unit and key not in install and key not in service, key
    assert install.get('WantedBy') == ['docker.service'] and 'firewall_persist.py receipt' in service['ExecStart'][0]
    assert 'ExecStartPre' not in service and 'Restart' not in service


# spec: ANYM-688
def test_check_reports_open_and_blocked_targets_with_a_positive_control():
    with socket.socket() as closed:
        # A port nobody holds: every OS refuses it.
        closed.bind(('127.0.0.1', 0))
        refused_target = f'127.0.0.1:{closed.getsockname()[1]}'
    with socket.socket() as listener, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as resolver, \
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as silent:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        resolver.bind(('127.0.0.1', 0))
        silent.bind(('127.0.0.1', 0))
        listening = f'127.0.0.1:{listener.getsockname()[1]}'
        answering = f'127.0.0.1:{resolver.getsockname()[1]}'

        def answer():
            query, peer = resolver.recvfrom(512)
            resolver.sendto(query[:2] + b'\x81\x80' + query[4:], peer)
        threading.Thread(target=answer, daemon=True).start()
        # The worker's view: the database and the mini open; TEST-NET-1 has no answer, what a DROP looks like from
        # inside; an alias that does not resolve and a resolver that never answers (a local silent socket: a real
        # address may sit behind a DNS-intercepting network) are blocked too.
        code, out, _ = run('check', '--timeout', '1', '--open', listening, '--blocked', '192.0.2.1:8080',
                           '--blocked-name', 'api.invalid:8080', '--blocked-dns', f'127.0.0.1:{silent.getsockname()[1]}')
        report = json.loads(out)
        assert code == 0 and report['passed'] is True
        assert [(r['expect'], r['passed']) for r in report['results']] == [
            ('open', True), ('blocked', True), ('blocked-name', True), ('blocked-dns', True)]
        assert report['results'][0]['outcome'] == 'connected' and report['results'][2]['outcome'] == 'unresolved'
        # The positive control, run from a sibling container: the same listener must connect, so a "blocked" in the
        # worker's run cannot mean that nothing listens. A control nobody answers fails, and so does the run.
        code, out, _ = run('check', '--timeout', '1', '--control', listening)
        assert code == 0 and json.loads(out)['results'][0] == {
            'target': listening, 'expect': 'control', 'outcome': 'connected', 'passed': True}
        code, out, _ = run('check', '--timeout', '1', '--control', refused_target)
        assert code == 1 and json.loads(out)['results'][0]['outcome'] == 'refused'
        # Every way an exit can be open fails: a connection, a refusal (something answered), a resolved name, an
        # answered DNS query. Names and malformed targets never pass as an IP literal.
        code, out, _ = run('check', '--timeout', '1', '--blocked', listening, '--blocked', refused_target,
                           '--blocked-name', 'localhost:' + listening.rpartition(':')[2], '--blocked-dns', answering,
                           '--blocked', 'api:8080', '--open', 'db.invalid:5432')
        report = json.loads(out)
        assert code == 1 and report['passed'] is False
        assert [r['passed'] for r in report['results']] == [False] * 6
        # A name that resolves fails whatever the connection would do: DNS escaped even if TCP is then dropped.
        assert [r['outcome'] for r in report['results']] == [
            'connected', 'refused', 'resolved', 'answered', 'malformed', 'unresolved']
        # An empty check proves nothing.
        code, out, _ = run('check')
        assert code == 1 and json.loads(out) == {'passed': False, 'results': []}
        # `listen` is the synthetic listener for the control: it prints its port, then accepts a connection.
        process = subprocess.Popen([sys.executable, '-I', str(SCRIPT), 'listen', '--port', '0', '--seconds', '10'],
                                   stdout=subprocess.PIPE, text=True)
        assert process.stdout is not None
        try:
            port = json.loads(process.stdout.readline())['listening']
            with socket.create_connection(('127.0.0.1', port), timeout=5):
                pass
        finally:
            process.terminate()
            process.wait(timeout=10)
            process.stdout.close()
        # In-process: a name that resolves fails even when the connection to its address is then dropped (the
        # lookup itself proves a resolver answered from inside), and an unresolved name passes.
        import importlib.util
        spec = importlib.util.spec_from_file_location('inference_egress_check', SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        original = module.socket.getaddrinfo
        module.socket.getaddrinfo = lambda *args, **kwargs: [(2, 1, 6, '', ('192.0.2.1', 8080))]
        try:
            result = module.check([('leak.example:8080', 'blocked-name')], 1)['results'][0]
        finally:
            module.socket.getaddrinfo = original
        assert (result['outcome'], result['passed']) == ('resolved', False)
