"""Steps the Telegram and WhatsApp collectors share: the opening checks of their configuration, the retry
delay of a loop that survives database errors, and the per-item commit of a history page.

Every function keeps the order of the statements it replaced, so the first failing check and the first side
effect stay where they were in each collector.
"""
import asyncio
import re


def validate_common(cfg,fields):
    """The first three checks of both collector configurations: known keys only, enabled, a valid account."""
    if not isinstance(cfg,dict) or set(cfg)-fields: raise ValueError('invalid_collector_config')
    if cfg.get('enabled') is not True: raise RuntimeError('collector_disabled')
    if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,64}',cfg.get('account','')): raise ValueError('invalid_account')


def validate_ranges(cfg,ranges):
    """Integer settings with a default and bounds: (key, default, lo, hi); the default is stored in cfg."""
    for key,default,lo,hi in ranges:
        value=cfg.setdefault(key,default)
        if type(value) is not int or not lo<=value<=hi: raise ValueError('invalid_'+key)


class DbBackoff:
    """The retry delay of a collector loop after a database error: the owner's database_retry, doubled per
    consecutive failure up to LIMIT seconds, and back to database_retry after a pass without one."""
    LIMIT=60
    def __init__(self,owner):self.owner=owner;self.delay=owner.database_retry
    async def wait(self):
        await asyncio.sleep(self.delay);self.delay=min(self.LIMIT,self.delay*2)
    def reset(self):self.delay=self.owner.database_retry


async def commit_items(collector,entries,*,check,build,build_errors,put_errors,record,record_unstorable):
    """Commit one history page directly, entry by entry (the order both collectors had):
    check(entry) may raise (a page that is not this chat's fails the pass); build(entry) gives the spool item,
    a build_errors exception or a put_errors exception from the spool counts the entry as invalid and moves on
    via record_unstorable(entry); otherwise the item is applied, its spool file discarded (committed directly;
    draining the same bytes again would only repeat the work) and record(entry) called."""
    for entry in entries:
        check(entry)
        try:item=build(entry)
        except build_errors:
            collector.invalid_messages+=1;record_unstorable(entry);continue
        try:name=await asyncio.to_thread(collector.history_spool.put,item)
        except put_errors:
            collector.invalid_messages+=1;record_unstorable(entry);continue
        await asyncio.to_thread(collector.state.apply,item)
        await asyncio.to_thread(collector.history_spool.discard,name)
        record(entry)
