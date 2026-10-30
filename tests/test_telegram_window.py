"""Date-window boundaries, widening, catalog visibility and restart fairness."""
import asyncio
from datetime import datetime,timezone

import pytest

from messaging_service.telegram_collector import history_bound
from test_collectors import worker as worker, message


def at(mid,date,chat='100',media=False):
    item=message(mid,chat=chat,media=media)
    item.date=datetime.fromisoformat(date).replace(tzinfo=timezone.utc)
    return item


def test_timezone_boundary_and_reject_naive():
    assert history_bound('2026-07-13T00:00:00+08:00')==datetime(2026,7,12,16,tzinfo=timezone.utc)
    with pytest.raises(ValueError,match='invalid_history_since'): history_bound('2026-07-13')


def test_crossing_window_does_not_claim_full_history_and_can_expand(worker):
    w,s,key=worker
    w.history_since=history_bound('2026-07-13T00:00:00Z')
    w.batch_size=3
    w.provider.messages=[at(1,'2026-06-01'),at(2,'2026-07-12'),at(3,'2026-07-13'),at(4,'2026-08-01')]
    asyncio.run(w.sync_once())
    assert {x['body'] for x in s.search(key)['items']}=={'message 3','message 4'}
    state=w.state.chat('100','user')
    assert state['window_complete'] and not state['history_exhausted'] and state['history_before']==3
    reads=len(w.provider.reads)
    asyncio.run(w.sync_once())
    assert not any(before for _,_,before,_ in w.provider.reads[reads:])
    w.history_since=history_bound('2026-01-01T00:00:00Z')
    asyncio.run(w.sync_once())
    assert {x['body'] for x in s.search(key)['items']}=={f'message {n}' for n in range(1,5)}
    assert w.state.chat('100','user')['history_exhausted']


def test_all_old_initial_page_keeps_cursor_for_future_widening(worker):
    w,s,key=worker
    w.history_since=history_bound('2026-07-13T00:00:00Z')
    w.provider.messages=[at(1,'2025-01-01'),at(2,'2025-01-02')]
    asyncio.run(w.sync_once())
    assert s.search(key)['items']==[]
    assert len(s.list_chats(key)['items'])==1
    state=w.state.chat('100','user')
    assert state['window_complete'] and not state['history_exhausted'] and state['history_before']==3
    w.history_since=None
    asyncio.run(w.sync_once())
    assert len(s.search(key)['items'])==2


def test_full_catalog_persisted_before_history_and_attempt_survives_failure(worker):
    w,s,key=worker
    async def dialogs(limit):
        assert limit is None
        return [('100','user','First'),('200','user','Second'),('300','user','Third')]
    w.provider.dialogs=dialogs
    attempts=[]
    async def page(chat,**kwargs):
        assert len(s.list_chats(key)['items'])==3
        attempts.append(chat)
        raise RuntimeError('interrupted')
    w.provider.page=page
    for _ in range(3):
        with pytest.raises(RuntimeError,match='interrupted'): asyncio.run(w.sync_once())
    assert attempts==['100','200','300']


def test_existing_old_media_remains_queued_but_not_downloaded(worker):
    w,s,key=worker
    from types import SimpleNamespace
    asyncio.run(w.capture(SimpleNamespace(message=at(10,'2025-01-01',media=True))))
    w.spool.drain(w.state.apply)
    w.history_since=history_bound('2026-07-13T00:00:00Z')
    assert not asyncio.run(w.media_once())
    with s.pool.connection() as c:
        assert c.execute('SELECT state,attempts FROM media_jobs WHERE account=%s',(w.account,)).fetchone()=={'state':'queued','attempts':0}
    w.history_since=None
    assert asyncio.run(w.media_once())


def test_catalog_respects_excluded_chats(worker):
    w,s,key=worker
    with s.pool.connection() as c:
        c.execute('UPDATE account_policies SET exclude_chats=%s WHERE account=%s',(['200'],w.account))
    async def dialogs(limit):return [('100','user','Allowed'),('200','user','Excluded')]
    w.provider.dialogs=dialogs
    asyncio.run(w.sync_once())
    assert [r['title'] for r in s.list_chats(key)['items']]==['Allowed']
    with s.pool.connection() as c:
        assert c.execute('SELECT count(*) n FROM collector_chats WHERE account=%s',(w.account,)).fetchone()['n']==1
