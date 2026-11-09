"""Telegram polls (ANYM-920): capture, pushed counts and the bounded public voter read.

A MessageMediaPoll becomes `metadata.poll` (question, options, flags) and the transient `poll_results` the archive
applies (polls.py). Each pushed UpdateMessagePoll is journaled as one `telegram_poll_results` capture, applied by poll
ID. For a poll Telegram marks public, a background pass reads its voters through the governor (at most once per poll
per 10 minutes, polls younger than 30 days, up to 200 votes) and journals them as one `telegram_poll_votes` capture;
an anonymous poll is never read. Poll texts and names are member-controlled data, stored bounded, never instructions.
Logs carry reason codes and counts only.

On by default since schema 84 is the release a rollback returns to; ANYMSG_CAPTURE_TELEGRAM_POLLS=0 is the kill switch.
A rollback past schema 84 sets it first (Astra r1 B4): a schema-83 collector rejects a journaled message carrying the
Telegram poll block and quarantines the whole message, as it does the two new capture kinds.
"""
import asyncio
from contextlib import suppress
from datetime import datetime,timezone
import json
import logging
import os
import time

from .adapters import display_text
from .polls import (MAX_OPTION_TEXT,MAX_OPTIONS,MAX_POLL_VOTES,MAX_QUESTION,MAX_VOTER_NAME,VOTES_READ_EVERY,
    due_telegram_polls,poll_block,telegram_results,validate_telegram_update)
from .spool import InvalidCapture
from .telegram_governor import BACKGROUND,ProviderRateLimited

POLL_VOTES_PAGE=50
POLL_VOTES_PASS=10
# One governed voter-list page holds the shared non-send lane at most this long (Astra r1 B1).
POLL_VOTES_TIMEOUT=30


def telegram_poll_capture():
    """The poll block, pushed counts and voter reads, on unless ANYMSG_CAPTURE_TELEGRAM_POLLS=0 (Astra r1 B4)."""
    return os.environ.get('ANYMSG_CAPTURE_TELEGRAM_POLLS')!='0'


class PollVotesRefused(Exception):
    """Telegram refused one poll's voter list (not a rate limit): the read is recorded, the throttle holds."""


def _poll_text(value,maximum):
    text=getattr(value,'text',value)
    text=display_text(text,maximum+1)
    return text[:maximum],len(text)>maximum


def _results(results,keys):
    """Per option key its count and the owner's chosen flag; null counts when Telegram sent none."""
    from telethon import types
    if not isinstance(results,types.PollResults):return None,None,True,[]
    total=results.total_voters if type(results.total_voters) is int and 0<=results.total_voters<2**31 else None
    items=[] if results.results is None else [r for r in results.results if isinstance(r,types.PollAnswerVoters)]
    by={bytes(r.option).hex():r for r in items}
    voters=None if results.results is None else [
        (by[k].voters if type(by[k].voters) is int and by[k].voters>=0 else 0) if k in by else 0 for k in keys]
    chosen=[i for i,k in enumerate(keys) if k in by and by[k].chosen is True]
    # Without per-option results Telegram states no choice either: the stored one stays (Astra r1 M5).
    return voters,total,results.min is True or results.results is None,chosen


def telegram_poll(media):
    """(metadata.poll, poll_results) of a MessageMediaPoll, or (None, None) when the poll cannot be read: the message
    is still stored with its kind, and the dropped block is logged with its reason code."""
    from telethon import types
    try:
        poll=getattr(media,'poll',None)
        if not isinstance(poll,types.Poll) or type(poll.id) is not int:raise ValueError('invalid_metadata')
        answers=[a for a in (poll.answers or []) if isinstance(a,types.PollAnswer)]
        question,cut=_poll_text(poll.question,MAX_QUESTION)
        options,keys=[],[]
        for index,answer in enumerate(answers[:MAX_OPTIONS]):
            text,answer_cut=_poll_text(answer.text,MAX_OPTION_TEXT);cut=cut or answer_cut
            options.append({'index':index,'text':text});keys.append(bytes(answer.option).hex())
        block=poll_block({'provider_poll_id':str(poll.id%2**64),'question':question,'options':options,
            'multiple_choice':poll.multiple_choice is True,'public_voters':poll.public_voters is True,
            'quiz':poll.quiz is True,'closed':poll.closed is True,'truncated':cut or len(answers)>MAX_OPTIONS})
        voters,total,minimal,chosen=_results(getattr(media,'results',None),keys)
        results=telegram_results({'observed_at':datetime.now(timezone.utc).isoformat(),'option_keys':keys,
            'voters':voters,'total_voters':total,'chosen':chosen,'min':minimal},block)
        return block,results
    except (AttributeError,TypeError,ValueError):
        logging.getLogger(__name__).warning('poll_block_dropped: invalid_metadata')
        return None,None


def poll_update_capture(account,update,observed_at):
    """One pushed UpdateMessagePoll as a journal entry: the poll ID, per option key its count and chosen flag."""
    from telethon import types
    results=update.results
    items=total=None;minimal=True
    if isinstance(results,types.PollResults):
        total=results.total_voters if type(results.total_voters) is int and 0<=results.total_voters<2**31 else None
        minimal=results.min is True
        if results.results is not None:
            items=[{'option':bytes(r.option).hex(),'voters':r.voters if type(r.voters) is int and r.voters>=0 else 0,
                'chosen':r.chosen is True} for r in results.results[:32] if isinstance(r,types.PollAnswerVoters)]
    return validate_telegram_update({'kind':'telegram_poll_results','account':account,
        'poll_provider_id':str(update.poll_id%2**64),'results':items,'total_voters':total,'min':minimal,
        'observed_at':observed_at})


def _peer_name(entity):
    from telethon import types
    if isinstance(entity,types.User):
        return display_text(' '.join(x for x in (entity.first_name,entity.last_name) if isinstance(x,str)),MAX_VOTER_NAME)
    return display_text(getattr(entity,'title',None),MAX_VOTER_NAME)


class PollProvider:
    """TelethonProvider's poll update subscription and voter read."""
    def subscribe_polls(self,handler):
        from telethon import events,types
        self.client.add_event_handler(handler,events.Raw(types.UpdateMessagePoll))

    async def poll_votes(self,chat,message_id,*,limit=MAX_POLL_VOTES):
        """Up to `limit` votes of one public poll, paged, each page one governed BACKGROUND request: a FloodWait
        raises ProviderRateLimited; any other refusal raises PollVotesRefused; a page that takes longer than
        POLL_VOTES_TIMEOUT is cancelled inside the governed call, so the lane is released, and raises TimeoutError.
        Returns ([(peer ID, name, option bytes)], complete): complete when Telegram had no further page."""
        from telethon import types,utils
        from telethon.errors import FloodError,FloodWaitError,RPCError
        from telethon.tl.functions.messages import GetPollVotesRequest
        votes,offset,complete=[],None,False
        while len(votes)<limit:
            async def read(offset=offset,size=min(POLL_VOTES_PAGE,limit-len(votes))):
                try:
                    async with asyncio.timeout(POLL_VOTES_TIMEOUT):
                        peer=await self.client.get_input_entity(int(chat))
                        return await self.client(GetPollVotesRequest(peer=peer,id=message_id,limit=size,offset=offset))
                except (FloodWaitError,FloodError,TimeoutError):raise
                except (RPCError,ValueError) as exc:raise PollVotesRefused(type(exc).__name__) from exc
            page=await self.rpc(read,'telegram_poll_votes',tier=BACKGROUND)
            names={utils.get_peer_id(x):_peer_name(x) for x in [*(page.users or []),*(page.chats or [])]}
            for vote in page.votes or []:
                if isinstance(vote,types.MessagePeerVote):options=[vote.option]
                elif isinstance(vote,types.MessagePeerVoteMultiple):options=list(vote.options)
                else:continue
                peer=utils.get_peer_id(vote.peer)
                votes.append((peer,names.get(peer,''),options))
            offset=page.next_offset
            if not offset or not page.votes:
                complete=True;break
        return votes[:limit],complete and len(votes)<=limit


class PollCollector:
    """TelegramCollector's poll handler and its background voter pass."""
    poll_votes_interval=60

    async def poll_update(self,update):
        # Taken before any await: the order Telegram delivered the updates in (the stored counts' order guard).
        observed=datetime.now(timezone.utc).isoformat()
        if self.stopping.is_set() or self.failed.is_set() or not telegram_poll_capture():return
        try:
            peer=getattr(update,'peer',None)
            if peer is not None:
                from telethon import utils
                if self.policy and not self.state.allows(self.policy,str(utils.get_peer_id(peer))):return
            capture=poll_update_capture(self.account,update,observed)
        except (AttributeError,TypeError,ValueError):
            self.reject('invalid_poll_update');return
        if not await self.capture_slot():return
        try:await asyncio.to_thread(self.spool.put,capture)
        except InvalidCapture as exc:self.reject(str(exc))
        except Exception as exc:self.capture_failed(exc)
        finally:self.release_capture()

    async def poll_voters_loop(self):
        """Never ends the collector: a failed pass is logged by class and the next one runs after the interval."""
        while not self.stopping.is_set():
            try:await self.poll_voters_pass()
            except asyncio.CancelledError:raise
            except Exception as exc:
                with suppress(Exception):logging.getLogger(__name__).warning('poll_votes_failed: %s',type(exc).__name__)
            self.poll_counts_report()
            with suppress(TimeoutError):
                async with asyncio.timeout(self.poll_votes_interval):await self.stopping.wait()

    def poll_counts_report(self):
        """The pass's counts as one stdout line; best effort, never an error of the loop (Astra r1 B3)."""
        try:
            counts=getattr(self.state,'poll_counts',None)
            if counts:
                self.state.poll_counts={}
                print(json.dumps({'event':'telegram_polls',**counts},sort_keys=True),flush=True)
        except Exception:pass

    def poll_allowed(self,chat):
        policy=self.policy
        return bool(policy) and policy.get('enabled',True) is not False and self.state.allows(policy,chat)

    async def poll_voters_pass(self,limit=POLL_VOTES_PASS):
        """Read the voters of the due public polls the current policy allows (checked again before journaling; Astra
        r1 M1), once per poll ID (Astra r1 M3); a FloodWait or a page timeout stops the pass."""
        if not telegram_poll_capture():return
        due=await asyncio.to_thread(due_telegram_polls,self.state,limit)
        reads=self.__dict__.setdefault('_poll_reads',{})
        now=time.monotonic()
        for key in [k for k,at in reads.items() if now-at>=VOTES_READ_EVERY.total_seconds()]:del reads[key]
        me=getattr(getattr(self.provider,'verified_self',None),'id',None)
        for poll in due:
            if self.stopping.is_set() or self.failed.is_set():return
            if poll['provider_poll_id'] in reads or not self.poll_allowed(poll['chat']):continue
            reads[poll['provider_poll_id']]=time.monotonic()
            observed=datetime.now(timezone.utc).isoformat();message_id=int(poll['provider_id']);complete=False
            try:found,complete=await self.provider.poll_votes(poll['chat'],message_id,limit=MAX_POLL_VOTES)
            except (ProviderRateLimited,TimeoutError):return
            except PollVotesRefused:votes=None
            else:
                index={key:i for i,key in enumerate(poll['option_keys'])};votes,seen=[],set()
                for peer,name,options in found:
                    voter=str(peer)
                    if voter in seen:continue
                    seen.add(voter)
                    chosen=sorted({index[k] for k in (bytes(o).hex() for o in options) if k in index})
                    votes.append({'voter':voter,'name':name,'options':chosen,'from_me':me is not None and peer==me})
            if not self.poll_allowed(poll['chat']):continue
            capture={'kind':'telegram_poll_votes','account':self.account,'chat':poll['chat'],'message_id':message_id,
                'votes':votes,'complete':complete,'observed_at':observed}
            try:await asyncio.to_thread(self.spool.put,capture)
            except InvalidCapture as exc:self.reject(str(exc))
