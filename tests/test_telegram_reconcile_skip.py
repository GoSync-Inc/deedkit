"""History reconcile skips the pages of a Telegram chat whose dialog shows nothing newer than its cursor.

Fake providers only: no credentials, client, network or live session."""
import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

from test_collectors import message, worker as worker


def passes(w,count=1):
    for _ in range(count):asyncio.run(w.sync_once())


def settled(w):
    """Initialize the fixture chat and finish its window backfill (5 messages, pages of 2)."""
    for _ in range(5):passes(w)
    with w.archive.store.pool.connection() as c:
        row=c.execute("SELECT high_water,history_exhausted,last_head_scan_at IS NOT NULL AS head FROM collector_chats"
            " WHERE account=%s AND provider_chat='100'",(w.account,)).fetchone()
    assert row=={'high_water':5,'history_exhausted':True,'head':True}
    w.provider.reads.clear()


HEAD=('100',5,0,True)  # the head page above the cursor
OVERLAP=('100',0,0,False)  # the recent-overlap page


def seen(top,age=60):
    """A dialog top as discovery records it: id, wall time and monotonic time of the catalog page."""
    return (top,datetime.now(timezone.utc)-timedelta(seconds=age),time.monotonic()-age)


def head_scan(w):
    with w.archive.store.pool.connection() as c:
        return c.execute("SELECT last_head_scan_at FROM collector_chats WHERE account=%s AND provider_chat='100'",
            (w.account,)).fetchone()['last_head_scan_at']


# spec: ANYM-103
def test_an_unchanged_chat_reads_only_its_overlap_page_and_keeps_the_catalog_as_head_evidence(worker):
    w,_,_=worker;settled(w)
    before=head_scan(w)
    w.provider.dialog_tops={'100':seen(5,age=0)}  # a catalog page seen after the last head scan
    passes(w,3)
    # The newest page is still reread every pass (ANYM-103); only the head page above the cursor is skipped.
    assert w.provider.reads==[OVERLAP]*3
    # The catalog's time is the head evidence: later than the old scan, never later than now.
    assert before<head_scan(w)<=datetime.now(timezone.utc)
    assert abs((head_scan(w)-w.provider.dialog_tops['100'][1]).total_seconds())<1
    fresh=head_scan(w)
    w.provider.dialog_tops={'100':seen(5,age=600)};passes(w)
    assert head_scan(w)==fresh  # an older catalog never moves it back


# spec: ANYM-103
def test_an_edit_inside_the_newest_page_of_an_unchanged_chat_is_still_read_every_pass(worker):
    w,s,key=worker;settled(w)
    w.provider.dialog_tops={'100':seen(5)}
    w.provider.messages=[m for m in w.provider.messages if m.id!=4]+[message(4,body='edited synthetic body',
        edited=datetime(2026,10,2,tzinfo=timezone.utc))]
    passes(w)
    assert w.provider.reads==[OVERLAP]
    with s.pool.connection() as c:
        assert c.execute("SELECT body FROM messages WHERE account=%s AND provider_id='4'",(w.account,)).fetchone()[
            'body']=='edited synthetic body'


# spec: ANYM-103
def test_a_chat_whose_dialog_top_is_newer_than_its_cursor_reads_as_before(worker):
    w,_,_=worker;settled(w)
    w.provider.messages+=[message(6),message(7)]
    w.provider.dialog_tops={'100':seen(7)}
    passes(w)
    assert w.provider.reads==[HEAD,('100',7,0,True),OVERLAP]
    with w.archive.store.pool.connection() as c:
        assert c.execute("SELECT high_water FROM collector_chats WHERE account=%s AND provider_chat='100'",
            (w.account,)).fetchone()['high_water']==7


# spec: ANYM-103
def test_an_old_or_unknown_dialog_top_still_reads_the_head(worker):
    from messaging_service.telegram_collector import TOP_FRESH_SECONDS
    w,_,_=worker;settled(w)
    passes(w)  # an older provider, or a dialog without a top message
    w.provider.dialog_tops={'200':seen(5)};passes(w)  # another chat's top says nothing about this one
    w.provider.dialog_tops={'100':seen(5,age=TOP_FRESH_SECONDS+1)};passes(w)  # a catalog too old to prove it
    assert w.provider.reads==[HEAD,OVERLAP]*3


# spec: ANYM-103
def test_a_pass_over_the_old_catalog_after_a_failed_discovery_reads_the_head(worker):
    from messaging_service.telegram_collector import DiscoveryFailed
    w,_,_=worker;settled(w)
    w.provider.dialog_tops={'100':seen(5)}
    listed=w.provider.dialogs
    async def failing(limit):raise DiscoveryFailed(RuntimeError('synthetic'))
    w.provider.dialogs=failing;w._next_discovery=0
    passes(w)  # discovery fails; history goes on over the cached catalog
    assert w.provider.reads==[HEAD,OVERLAP]
    w.provider.dialogs=listed;w._next_discovery=0;w.provider.reads.clear()
    passes(w)  # a good discovery trusts the catalog again
    assert w.provider.reads==[OVERLAP]


# spec: ANYM-103
def test_a_skipped_chat_still_backfills_its_window(worker):
    w,_,_=worker
    passes(w)  # initialized: messages 5 and 4, the window still open below 4
    w.provider.reads.clear();w.provider.dialog_tops={'100':seen(5)}
    passes(w)
    assert w.provider.reads==[OVERLAP,('100',0,4,False)]


# spec: ANYM-103
def test_discovery_records_each_dialogs_top_message_and_when_it_was_seen():
    from messaging_service.telegram_collector import TelethonProvider
    from messaging_service.telegram_governor import RequestGovernor
    from test_telegram_governor import Clock,State
    clock=Clock();at=datetime(2026,11,1,tzinfo=timezone.utc)
    class Dialogs:
        def __init__(self):self.buffer=None;self.index=0
        def __aiter__(self):return self
        async def __anext__(self):
            if self.buffer is None:
                self.buffer=[NS(id=1,is_channel=False,is_user=True,is_group=False,name='fixture',date=at,
                    dialog=NS(top_message=9,unread_count=0,read_inbox_max_id=9)),
                    NS(id=2,is_channel=False,is_user=True,is_group=False,name='fixture',date=None,dialog=None)]
            if self.index==len(self.buffer):raise StopAsyncIteration
            item=self.buffer[self.index];self.index+=1;return item
    provider=TelethonProvider.__new__(TelethonProvider)
    provider.client=NS(iter_dialogs=lambda **_:Dialogs())
    provider.governor=RequestGovernor(State(clock),interval=0,clock=clock,sleep=clock.sleep)
    before=datetime.now(timezone.utc)
    assert [row[0] for row in asyncio.run(provider.dialogs(None))]==['1','2']
    assert set(provider.dialog_tops)=={'1'} and provider.dialog_tops['1'][0]==9
    assert before<=provider.dialog_tops['1'][1]<=datetime.now(timezone.utc)  # when the page was seen
