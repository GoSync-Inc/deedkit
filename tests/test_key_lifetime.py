"""Agent key lifetime ceiling of one year for issue-key and extend-key (ANYM-499)."""
from datetime import timedelta

import pytest

from messaging_service import __main__ as operator
from messaging_service.store import digest


def left(store, token):
    with store.pool.connection() as c:
        return c.execute('SELECT expires_at-clock_timestamp() AS left FROM api_keys WHERE digest=%s',
                         (digest(token),)).fetchone()['left']


def keys(store):
    with store.pool.connection() as c:
        return c.execute('SELECT id,expires_at FROM api_keys ORDER BY id').fetchall()


# spec: ANYM-499
@pytest.mark.parametrize('days', [31, 184, 365])
def test_issue_key_accepts_up_to_365_days(seeded, days):
    store, _, _ = seeded
    token = store.issue_key('agent-year', ['telegram-demo'], days=days)
    assert timedelta(days=days) - timedelta(seconds=5) < left(store, token) <= timedelta(days=days)
    assert store.capabilities(token)['credential_id']


# spec: ANYM-499
def test_issue_key_default_stays_seven_days(seeded):
    store, _, _ = seeded
    token = store.issue_key('agent-default', ['telegram-demo'])
    assert timedelta(days=7) - timedelta(seconds=5) < left(store, token) <= timedelta(days=7)


# spec: ANYM-499
@pytest.mark.parametrize('days', [0, -1, 366, 1000, True, 1.5, '365', None])
def test_issue_key_refuses_lifetime_outside_one_to_365_days(seeded, days):
    store, _, _ = seeded
    before = keys(store)
    with pytest.raises(ValueError, match='^invalid_key_lifetime$'):
        store.issue_key('agent-year', ['telegram-demo'], days=days)
    assert keys(store) == before


# spec: ANYM-499
def test_extend_key_accepts_365_days_and_refuses_366(seeded):
    store, token, _ = seeded
    key = store.capabilities(token)['credential_id']
    with pytest.raises(ValueError, match='^invalid_key_lifetime$'):
        store.extend_key(key, 366)
    before = left(store, token)
    assert before <= timedelta(days=7)
    store.extend_key(key, 365)
    assert timedelta(days=365) - timedelta(seconds=5) < left(store, token) <= timedelta(days=365)


# spec: ANYM-499
def test_cli_help_states_the_365_day_ceiling(capsys, monkeypatch):
    monkeypatch.setattr('sys.argv', ['anymsg', '--help'])
    with pytest.raises(SystemExit):
        operator.main()
    assert 'issue-key: 1-365 (default 7); extend-key: required, 1-365' in ' '.join(capsys.readouterr().out.split())
