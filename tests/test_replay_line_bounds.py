"""The operator replay's line bound: 512 KiB of payload plus its newline is read, one byte more is refused (ANYM-98).

`replay` reads each line with readline(MAX_ENVELOPE + 2) and refuses a line longer than MAX_ENVELOPE + 1 bytes. The
existing replay test checks only an oversized line. Here a valid synthetic envelope is padded with JSON whitespace
to exactly MAX_ENVELOPE payload bytes, so the line bound itself is what decides. The canonical envelope the spool
stores is far smaller and is bounded separately by envelope validation. No collector, provider or transport runs.
"""
import json
import sys

import pytest

from messaging_service import __main__ as cli
from messaging_service.envelope import MAX_ENVELOPE
from test_archive import event

ACCOUNT = 'replay-bounds'


def padded_line(payload_bytes, provider_id):
    """One JSON envelope padded with spaces to exactly `payload_bytes`, then a newline."""
    body = json.dumps(event(account=ACCOUNT, provider_id=provider_id, origin='fixture')).encode()
    assert len(body) < payload_bytes
    return body + b' ' * (payload_bytes - len(body)) + b'\n'


@pytest.fixture()
def replay(seeded, tmp_path, monkeypatch, capsys):
    store, _, _ = seeded
    from messaging_service.archive import Archive
    Archive(store).register(ACCOUNT, 'telegram', 'Replay bounds', discover=True)
    dsn = tmp_path / 'test.dsn'; dsn.write_text(store.pool.conninfo); dsn.chmod(0o600)

    class BorrowedStore:
        def __getattr__(self, name):
            return getattr(store, name)

        def close(self):
            pass  # The seeded fixture owns this connection pool.
    monkeypatch.setattr(cli, 'Store', lambda *_: BorrowedStore())
    monkeypatch.setenv('DATABASE_URL_FILE', str(dsn))
    spool = tmp_path / 'spool'

    def run(data):
        infile = tmp_path / 'input.jsonl'; infile.write_bytes(data)
        monkeypatch.setattr(sys, 'argv', ['anymsg', 'replay', '--input', str(infile), '--spool', str(spool)])
        cli.main()
        return json.loads(capsys.readouterr().out)

    def messages():
        with store.pool.connection() as c:
            return [row['provider_id'] for row in c.execute(
                'SELECT provider_id FROM messages WHERE account=%s ORDER BY provider_id', (ACCOUNT,)).fetchall()]
    return run, messages, spool


# spec: ANYM-98
def test_a_line_of_exactly_512_kib_payload_and_its_newline_is_replayed_once(replay):
    run, messages, spool = replay
    line = padded_line(MAX_ENVELOPE, 'edge-accepted')
    assert len(line) == MAX_ENVELOPE + 1 and line.endswith(b'\n')
    assert run(line) == {'queued': 1, 'final_drain': 0}  # drained by the pass after its put
    assert messages() == ['edge-accepted'] and not list(spool.glob('*.json'))
    # The same line again is deduplicated, not stored twice.
    assert run(line) == {'queued': 1, 'final_drain': 0}
    assert messages() == ['edge-accepted']


# spec: ANYM-98
def test_one_payload_byte_more_is_refused_before_anything_is_spooled(replay):
    run, messages, spool = replay
    accepted = padded_line(MAX_ENVELOPE, 'before-the-long-line')
    longer = padded_line(MAX_ENVELOPE + 1, 'one-byte-over')
    assert len(longer) == MAX_ENVELOPE + 2
    with pytest.raises(ValueError, match='^envelope_too_large$'):
        run(accepted + longer)
    # The earlier line is replayed; the long one never reaches the spool or the archive.
    assert messages() == ['before-the-long-line'] and not list(spool.glob('*.json'))
