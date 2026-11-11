"""Temporary probe (removed before commit): which indexes the media pick plan uses, over repeated runs."""
import json
import re

import pytest

from messaging_service import archive as archive_module
from test_archive import archive  # noqa: F401
from test_media_claim_performance import backlog, explained, grow_dormant, message  # noqa: F401


@pytest.mark.parametrize('full', [False, True])
@pytest.mark.parametrize('round_', range(10))
def test_probe(backlog, round_, full):  # noqa: F811
    ar, s, claim, _ = backlog
    grow_dormant(s, 30_000)
    assert claim() is None
    target = message(ar, 'wanted', 90*86400)
    with s.pool.connection() as c:
        c.execute("UPDATE media_jobs SET requested_until=clock_timestamp()+interval '1 hour' WHERE message=%s", (target,))
        if full: c.execute('SET LOCAL default_statistics_target=1000')
        c.execute('ANALYZE media_jobs'); c.execute('ANALYZE messages')
        stats = c.execute("SELECT attname,n_distinct,correlation FROM pg_stats WHERE tablename='messages' AND attname IN ('chat','sent_at')").fetchall()
    plans = []
    with explained(ar, plans): job = claim()
    plan = next(p for q, p in plans if archive_module.MEDIA_PICK_MARK in q)
    names = sorted(set(re.findall(r'"Index Name": "([a-z_]+)"', json.dumps(plan))))
    pytest.fail(f"PROBE media {'full' if full else 'default'} {round_} {job['message'] == target} {names} "
                f"{[dict(r) for r in stats]} {plan['Total Cost']}")
