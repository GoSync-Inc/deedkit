"""`deedkit-android engine login claude|codex`: the owner's one login step per engine (ANYM-972 criterion 4)."""

import os
import stat
import sys
import uuid

from agent_harness import write_config
from deedkit_android.config import Configuration
from deedkit_android.engine_probe import engine_main

CLAUDE = '''#!{python}
import sys, time
sys.stdout.write("Open https://example.test/oauth and approve.\\n")
sys.stdout.flush()
token = "{token}"
sys.stdout.write("Your token: " + token[:25])  # a token split across two writes
sys.stdout.flush()
time.sleep(0.3)
sys.stdout.write(token[25:] + "\\nStore it safely.\\n")
'''
CODEX = '''#!{python}
import os, pathlib
home = pathlib.Path(os.environ["CODEX_HOME"])
(home / "auth.json").write_text('{{"auth_mode": "chatgpt", "tokens": {{"refresh_token": "synthetic"}}}}')
(home / "auth.json").chmod(0o644)
pathlib.Path(os.environ["CODEX_HOME"] + ".called").write_text("login " + os.environ.get("ARGV_PROBE", ""))
'''


def binary(tmp_path, name, body):
    path = tmp_path / name
    path.write_text(body.format(python=sys.executable, token='sk-ant-oat01-' + uuid.uuid4().hex + uuid.uuid4().hex))
    path.chmod(0o755)
    return path


def configured(tmp_path, claude_body=None, codex_body=None):
    claude = binary(tmp_path, 'claude-login', claude_body or CLAUDE)
    codex = binary(tmp_path, 'codex-login', codex_body or CODEX)
    config = write_config(tmp_path, claude=claude, codex=codex)
    values = Configuration(config).read().engines
    os.unlink(values['claude'].token_file)  # no login yet
    for entry in os.scandir(values['codex'].codex_home):
        os.unlink(entry.path)
    return config, values, claude


# spec: ANYM-972
def test_login_claude_writes_the_token_file_0600_and_never_echoes_the_token(tmp_path, capfd):
    config, values, claude = configured(tmp_path)
    token = next(line for line in claude.read_text().splitlines() if line.startswith('token = ')).split('"')[1]
    assert engine_main(['login', 'claude', '--config', str(config)]) == 0
    shown = capfd.readouterr()
    path = values['claude'].token_file
    assert open(path).read().strip() == token and stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert token not in shown.out + shown.err and token[:25] not in shown.out + shown.err
    assert 'Open https://example.test/oauth' in shown.out and 'token hidden' in shown.out


# spec: ANYM-972
def test_login_claude_without_a_printed_token_or_with_a_failed_exit_writes_nothing(tmp_path, capfd):
    silent = '#!{python}\nprint("approved")\n'
    failing = '#!{python}\nimport sys\nprint("sk-ant-oat01-" + "a" * 40)\nsys.exit(3)\n'
    for body, code in ((silent, 'token_not_found'), (failing, 'login_failed')):
        folder = tmp_path / code
        folder.mkdir()
        config, values, _ = configured(folder, claude_body=body)
        assert engine_main(['login', 'claude', '--config', str(config)]) == 1
        assert code in capfd.readouterr().err
        assert not os.path.exists(values['claude'].token_file)


# spec: ANYM-972
def test_login_codex_runs_in_the_private_codex_home_and_leaves_auth_json_0600(tmp_path):
    config, values, _ = configured(tmp_path)
    home = values['codex'].codex_home
    assert engine_main(['login', 'codex', '--config', str(config)]) == 0
    assert stat.S_IMODE(os.stat(home + '/auth.json').st_mode) == 0o600 and stat.S_IMODE(os.stat(home).st_mode) == 0o700
    assert open(home + '.called').read().startswith('login')  # the binary ran with CODEX_HOME set to the private home


# spec: ANYM-972
def test_login_needs_the_engine_and_its_login_path_in_the_config(tmp_path, capfd):
    config = write_config(tmp_path)  # no engines configured
    assert engine_main(['login', 'claude', '--config', str(config)]) == 2
    assert 'engine_not_configured' in capfd.readouterr().err
