"""ANYM-18: a `codex://threads/<UUID>` result_ref is passive metadata. It is recorded and read back exactly, never
opened, and a malformed variant is refused before any write. Real record/read path on the disposable test database;
the openers are tripwired, so nothing reaches a socket, a process or a browser."""
import json
import socket
import subprocess
import urllib.request
import webbrowser

import pytest

from messaging_service.store import APIError
from test_reviews import payload
from test_reviews import reviewers  # noqa: F401 - shared review fixture

THREAD = '0b0b0b0b-0b0b-4b0b-8b0b-0b0b0b0b0b0b'
REFUSED = ['codex://threads/' + '-' * 36, 'codex://threads/' + THREAD.replace('-', ''),
           f'codex://threads/{THREAD}/x', f'codex://threads/{THREAD}?x=1', f'codex://threads/{THREAD}#x',
           f'codex://thread/{THREAD}', f'Codex://threads/{THREAD}', f'codex:/threads/{THREAD}',
           f'codex:///threads/{THREAD}', f'codex://threads/{THREAD[:-1]}g', f'codex://threads/{THREAD[:-1]}',
           'codex://threads/{' + THREAD[1:-1] + '}', f'codex://user@threads/{THREAD}', f'codex://threads/{THREAD} ']


class Opened(AssertionError):
    pass


@pytest.fixture()
def tripwire(monkeypatch):
    """Any network connection, process or browser started by the call under test fails the test."""
    def opened(*args, **kwargs):
        raise Opened('a result_ref was opened')
    for target, name in [(socket.socket, 'connect'), (socket, 'create_connection'), (subprocess, 'Popen'),
                         (urllib.request, 'urlopen'), (webbrowser, 'open')]:
        monkeypatch.setattr(target, name, opened)
    # Positive control: every entry point is armed.
    for call in (lambda: socket.socket().connect(('127.0.0.1', 9)), lambda: socket.create_connection(('127.0.0.1', 9)),
                 lambda: subprocess.Popen(['true']), lambda: urllib.request.urlopen('https://example.invalid'),
                 lambda: webbrowser.open(f'codex://threads/{THREAD}')):
        with pytest.raises(Opened):
            call()


def count(s):
    with s.pool.connection() as c:
        return c.execute('SELECT count(*) AS n FROM reviews').fetchone()['n']


# spec: ANYM-18
@pytest.mark.parametrize('ref', [f'codex://threads/{THREAD}', f'codex://threads/{THREAD.upper()}'], ids=['lower', 'upper'])
def test_a_codex_thread_ref_is_recorded_and_read_back_exactly_and_never_opened(reviewers, tripwire, ref):  # noqa: F811
    s, a, b = reviewers
    request = payload(result_ref=ref)
    first = s.record_review(b, **request)
    assert first['review']['result_ref'] == ref and first['replayed'] is False
    assert first['review']['assertion'] == 'agent_reported_review'
    again = s.record_review(b, **request)
    assert again['replayed'] is True and again['review']['result_ref'] == ref
    shared = s.get_context(a, 'demo-tg-1')['reviews']['items']  # another agent's ordinary read
    assert [review['result_ref'] for review in shared] == [ref]


# spec: ANYM-18
@pytest.mark.parametrize('ref', REFUSED)
def test_a_malformed_codex_thread_ref_is_refused_before_any_write(reviewers, tripwire, ref):  # noqa: F811
    s, a, b = reviewers
    with pytest.raises(APIError, match='invalid_review'):
        s.record_review(b, **payload(result_ref=ref))
    assert count(s) == 0
    audit = s.audit_tail(principal='agent-b', limit=1)[0]
    assert audit['operation'] == 'record_review' and audit['outcome'] == 'denied'
    assert 'codex' not in json.dumps(audit) and 'result_ref' not in json.dumps(audit)


# spec: ANYM-18
@pytest.mark.parametrize('ref', ['codex://threads/' + 'a' * 32 + '----', 'codex://threads/' + '----' + 'a' * 32],
                         ids=['hyphens-last', 'hyphens-first'])
def test_a_codex_thread_ref_needs_canonical_uuid_text_not_misplaced_hyphens(reviewers, tripwire, ref):  # noqa: F811
    # 32 hex digits and 4 hyphens in the wrong places still parse with UUID(), which ignores hyphen positions.
    s, a, b = reviewers
    with pytest.raises(APIError, match='invalid_review'):
        s.record_review(b, **payload(result_ref=ref))
    assert count(s) == 0
