"""The messaging operator commands behind `deedkit admin session-claim` (ANYM-914): the real compare-and-set."""
import json
import sys


import messaging_service.__main__ as cli

HOST, HOLDER = 'a' * 32, 'b' * 32


def operate(seeded, tmp_path, monkeypatch, capsys, *argv):
    store = seeded[0]

    class BorrowedStore:
        def __getattr__(self, name):
            return getattr(store, name)

        def close(self):
            pass

    dsn = tmp_path / 'test.dsn'
    dsn.write_text(store.pool.conninfo)
    dsn.chmod(0o600)
    monkeypatch.setattr(cli, 'Store', lambda *_: BorrowedStore())
    monkeypatch.setenv('DATABASE_URL_FILE', str(dsn))
    monkeypatch.setattr(sys, 'argv', ['anymsg', *argv])
    cli.main()
    return json.loads(capsys.readouterr().out)


def insert(store, restore_hold=False):
    with store.pool.connection() as c:
        c.execute('DELETE FROM session_claims')
        c.execute('''INSERT INTO session_claims(account,host_id,boot_id,lock_ref,holder_id,holder_kind,generation,claimed_at,
            seen_at,restore_hold) VALUES('telegram-demo',%s,'boot','1:2',%s,'collector',3,clock_timestamp(),clock_timestamp(),%s)''',
                  (HOST, HOLDER, restore_hold))


def state(store):
    with store.pool.connection() as c:
        return c.execute('SELECT generation,released_by,restore_hold FROM session_claims').fetchall()


# spec: ANYM-914
def test_the_operator_release_is_a_compare_and_set_and_clears_a_restore_hold(seeded, tmp_path, monkeypatch, capsys):
    store = seeded[0]
    insert(store, restore_hold=True)
    listed = operate(seeded, tmp_path, monkeypatch, capsys, 'session-claim-list', '--account', 'telegram-demo')
    assert listed['items'][0]['holder_id'] == HOLDER and listed['items'][0]['restore_hold'] is True
    release = ['session-claim-release', '--account', 'telegram-demo', '--holder-id', HOLDER, '--host-id', HOST]
    # A stale read (the generation moved) changes nothing; the matching read releases and clears the hold.
    assert operate(seeded, tmp_path, monkeypatch, capsys, *release, '--generation', '2')['released'] is False
    assert state(store) == [{'generation': 3, 'released_by': None, 'restore_hold': True}]
    assert operate(seeded, tmp_path, monkeypatch, capsys, *release, '--generation', '3')['released'] is True
    assert state(store) == [{'generation': 3, 'released_by': 'admin', 'restore_hold': False}]


# spec: ANYM-914
def test_the_operator_release_of_a_restored_account_without_a_row_inserts_a_released_one(seeded, tmp_path, monkeypatch, capsys):
    store = seeded[0]
    with store.pool.connection() as c:
        c.execute('DELETE FROM session_claims')
        c.execute("INSERT INTO source_status(account,status) VALUES('telegram-demo','restored_disabled') "
                  "ON CONFLICT(account) DO UPDATE SET status='restored_disabled'")
    listed = operate(seeded, tmp_path, monkeypatch, capsys, 'session-claim-list')
    assert listed == {'items': [], 'restored_without_claim': ['telegram-demo']}
    release = ['session-claim-release', '--account', 'telegram-demo']
    assert operate(seeded, tmp_path, monkeypatch, capsys, *release)['released'] is True
    assert state(store) == [{'generation': 1, 'released_by': 'admin', 'restore_hold': False}]
    # Not restored (or the row exists now): nothing is inserted.
    assert operate(seeded, tmp_path, monkeypatch, capsys, *release)['released'] is False
