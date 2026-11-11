"""Temporary probe (removed before commit): the courtesy scale test's visit counts over repeated runs."""
import json

import pytest

from test_queue_courtesies_scale import courtesy_catalogue, host_speed_apart, measure  # noqa: F401
from test_search_performance import scans, visited


@pytest.mark.parametrize('full', [False, True])
@pytest.mark.parametrize('round_', range(8))
def test_probe(seeded, round_, full):
    s, _, b = seeded
    courtesy_catalogue(s)
    if full:
        with s.pool.connection() as c:
            c.execute('SET LOCAL default_statistics_target=1000'); c.execute('ANALYZE')
    _, plain = measure(s, b)
    _, closing = measure(s, b, courtesies='exclude')
    base = visited(plain.plans[0]['Plan'], {'messages'})
    extra = visited(closing.plans[0]['Plan'], {'messages'}) - base
    counted = visited(closing.counts[0]['Plan'], {'messages'})
    tag = 'full' if full else 'default'
    detail = ''
    if extra > 63700 or counted > base + 63700 or round_ == 0:
        detail = '\nPLAIN ' + json.dumps(scans(plain.plans[0]['Plan'])) + '\nCLOSING ' + json.dumps(
            scans(closing.plans[0]['Plan'])) + '\nCOUNT ' + json.dumps(scans(closing.counts[0]['Plan']))
    pytest.fail(f'PROBE courtesy {tag} {round_} base {base} extra {extra} counted {counted}' + detail)
