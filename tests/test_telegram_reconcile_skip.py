"""History reconcile skips the pages of a Telegram chat whose dialog shows nothing newer than its cursor.

Fake providers only: no credentials, client, network or live session."""
import asyncio
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


QUIET=datetime.now(timezone.utc)-timedelta(days=3)
HEAD=('100',5,0,True)  # the head page above the cursor
OVERLAP=('100',0,0,False)  # the recent-overlap page


# spec: ANYM-103
def test_a_quiet_chat_whose_dialog_top_is_stored_reads_only_its_overlap_turn(worker):
    from messaging_service.telegram_collector import RECONCILE_OVERLAP_PASSES
    w,_,_=worker;settled(w)
    w.provider.dialog_tops={'100':(5,QUIET)}
    passes(w,RECONCILE_OVERLAP_PASSES)
    # No head page in any pass, and the overlap once in RECONCILE_OVERLAP_PASSES passes (its round-robin turn).
    assert w.provider.reads==[OVERLAP]


# spec: ANYM-103
def test_a_chat_whose_dialog_top_is_newer_than_its_cursor_reads_as_before(worker):
    w,_,_=worker;settled(w)
    w.provider.messages+=[message(6),message(7)]
    w.provider.dialog_tops={'100':(7,datetime.now(timezone.utc))}
    passes(w)
    assert w.provider.reads[:2]==[HEAD,('100',7,0,True)] and w.provider.reads[-1]==OVERLAP
    with w.archive.store.pool.connection() as c:
        assert c.execute("SELECT high_water FROM collector_chats WHERE account=%s AND provider_chat='100'",
            (w.account,)).fetchone()['high_water']==7


# spec: ANYM-103
def test_a_recently_active_unchanged_chat_skips_the_head_but_keeps_the_overlap(worker):
    w,_,_=worker;settled(w)
    w.provider.dialog_tops={'100':(5,datetime.now(timezone.utc)-timedelta(minutes=5))}
    passes(w,3)
    assert w.provider.reads==[OVERLAP]*3


# spec: ANYM-103
def test_a_chat_without_a_known_dialog_top_still_reads_every_page(worker):
    w,_,_=worker;settled(w)
    passes(w)  # an older provider, or a dialog without a top message
    assert w.provider.reads==[HEAD,OVERLAP]
    w.provider.reads.clear();w.provider.dialog_tops={'200':(5,QUIET)}
    passes(w)  # another chat's top says nothing about this one
    assert w.provider.reads==[HEAD,OVERLAP]


# spec: ANYM-103
def test_a_skipped_chat_still_backfills_its_window(worker):
    w,_,_=worker
    passes(w)  # initialized: messages 5 and 4, the window still open below 4
    w.provider.reads.clear();w.provider.dialog_tops={'100':(5,QUIET)}
    passes(w)
    # The older page still comes; the quiet chat's overlap may or may not be this pass's turn.
    assert ('100',0,4,False) in w.provider.reads and set(w.provider.reads)<={('100',0,4,False),OVERLAP}


# spec: ANYM-103
def test_discovery_records_each_dialogs_top_message_and_date():
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
    assert [row[0] for row in asyncio.run(provider.dialogs(None))]==['1','2']
    assert provider.dialog_tops=={'1':(9,at)}
