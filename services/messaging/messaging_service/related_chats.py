"""Heuristic hint on chat rows: which older private chat a brand-new private chat probably continues (ANYM-656).

The only basis is a copy: one of a new chat's first incoming messages repeats, after normalization, an owner message
sent in exactly one other private chat in the 7 days before it. Read-only: no schema, stored link or event. Everything
runs over the caller's visible corpus (`scope_filter`), inside one savepoint under one elapsed-time deadline; any error
or timeout drops every partial hint and the caller returns its ordinary rows.
"""
import json
import logging
import time
import unicodedata
from datetime import datetime, timedelta, timezone

from .policy import scope_filter

log=logging.getLogger('messaging_service.related_chats')
NEW_DAYS=7  # a chat is new while its first archived message (by message time) is this young
WINDOW_DAYS=7  # the owner message was sent at most this long before its copy
MIN_CHARS=24
MAX_CHECKED=5  # new chats checked per page
FIRST_INCOMING=3
MAX_CANDIDATES=200  # outgoing candidates per copy; more means uniqueness cannot be established
PREFIX=2048
BODY_READ=8192
DEADLINE_SECONDS=1.0
MAX_RELATED=3
TITLE=60
LIST_CHARS=600
KEYS=('related_chats','continued_by')


class Deadline(Exception):
    """The helper's one elapsed-time budget is spent."""


def normalize(text):
    """Unicode NFKC, casefolded, whitespace collapsed, cut to the 2 KB prefix."""
    return ' '.join(unicodedata.normalize('NFKC',text or '').casefold().split())[:PREFIX]


def usable(text):
    # Under 24 characters or a single token (a bare URL, number or address) proves nothing.
    return len(text)>=MIN_CHARS and ' ' in text


def _like(token):
    return '%'+token.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'


class _Run:
    def __init__(self,c):self.c=c;self.end=time.monotonic()+DEADLINE_SECONDS

    def check(self):
        if self.end-time.monotonic()<=0:raise Deadline('budget_exhausted')

    def rows(self,step,sql,args):
        # Each statement gets what is left of the one budget as its local statement timeout.
        self.check()
        left=max(1,int((self.end-time.monotonic())*1000))
        self.c.execute("SELECT set_config('statement_timeout',%s,true)",(str(left)+'ms',))
        return self.c.execute('/* related_chats:'+step+' */ '+sql,args).fetchall()


def hidden_side(aliases,shown):
    """The alias chat of each linked WhatsApp pair: the twin of a shown chat, else the higher id."""
    return {chat for chat,twin in aliases.items()
        if chat not in shown and (twin in shown or twin not in shown and chat>twin)}


def _fit(entries):
    # At most 3 entries and 600 characters; a list that cannot fit even one entry is dropped.
    entries=entries[:MAX_RELATED]
    while entries and len(json.dumps(entries,ensure_ascii=False,separators=(',',':'),default=str))>LIST_CHARS:entries.pop()
    return entries


def hints(c,p,entries,*,hidden=frozenset(),twins=None):
    """{chat id: {'related_chats': [...], 'continued_by': [...]}} for the page's rows; {} on any failure.

    entries: [{'id','type','last_activity'}] in page order. `hidden` are WhatsApp alias chats (never a candidate,
    a match or a count); `twins` maps a chat to its alias twin (not "another chat" for it).
    """
    floor=datetime.now(timezone.utc)-timedelta(days=NEW_DAYS)
    # A chat whose newest message is older than the window cannot have a first message inside it.
    todo=[e for e in entries if e['type']=='private' and (e.get('last_activity') is None or e['last_activity']>=floor)]
    if not todo:return {}
    try:
        with c.transaction():  # a savepoint: a failed statement leaves the caller's transaction usable
            return _find(c,p,todo,floor,set(hidden),twins or {})
    except Exception as failure:
        log.warning('related_chats_abandoned %s',type(failure).__name__)
        return {}


def _find(c,p,todo,floor,hidden,twins):
    run=_Run(c)
    previous=c.execute("SELECT current_setting('statement_timeout') AS v").fetchone()['v']
    chat_scope,chat_args=scope_filter(p,'c',chat_column='id')
    # The first message per direction reads message_reply_order (a bounded indexed walk from the chat's oldest message);
    # one ORDER BY over both directions would let the planner walk message_live_date across the whole archive.
    fresh={r['id']:r for r in run.rows('new','''SELECT c.id,c.title FROM chats c CROSS JOIN LATERAL (
        SELECT min(t.sent_at) AS sent_at FROM unnest(ARRAY['incoming','outgoing']) d CROSS JOIN LATERAL (
          SELECT m.sent_at FROM messages m WHERE m.account=c.account AND m.chat=c.id AND m.direction=d
            AND NOT m.deleted AND NOT m.purged ORDER BY m.sent_at LIMIT 1) t) f
        WHERE c.id=ANY(%s) AND c.chat_type='private' AND c.purged_at IS NULL AND '''+chat_scope+' AND f.sent_at>=%s',
        [[e['id'] for e in todo],*chat_args,floor])}
    new=[e['id'] for e in todo if e['id'] in fresh][:MAX_CHECKED]
    found={}
    if new:
        msg_scope,msg_args=scope_filter(p,'m')
        pairs=[]
        for r in run.rows('first','''SELECT n.chat,f.id,f.sent_at,left(f.body,%s) AS body FROM unnest(%s::text[]) AS n(chat)
            JOIN chats c ON c.id=n.chat CROSS JOIN LATERAL (SELECT m.id,m.sent_at,m.body FROM messages m
              WHERE m.account=c.account AND m.chat=c.id AND m.direction='incoming' AND NOT m.deleted AND NOT m.purged
                AND '''+msg_scope+' ORDER BY m.sent_at,m.id LIMIT '+str(FIRST_INCOMING)+') f',[BODY_READ,new,*msg_args]):
            text=normalize(r['body'])
            token=max(text.split(),key=len) if usable(text) else ''
            # The trigram index needs 3 characters; a body without one cannot be checked cheaply, so it is omitted.
            if len(token)>=3:pairs.append({'i':len(pairs),'chat':r['chat'],'id':r['id'],'at':r['sent_at'],'text':text,'token':token})
        if pairs:
            candidates={}
            for r in run.rows('candidates','''SELECT q.i,o.* FROM jsonb_to_recordset(%s::jsonb) AS q(i int,copy_at timestamptz,pattern text)
                CROSS JOIN LATERAL (SELECT m.id,m.chat,m.sent_at,left(m.body,%s) AS body,c.title,c.chat_type
                  FROM messages m JOIN chats c ON c.id=m.chat AND c.purged_at IS NULL
                  WHERE '''+msg_scope+''' AND m.direction='outgoing' AND NOT m.deleted AND NOT m.purged
                    AND m.sent_at<=q.copy_at AND m.sent_at>=q.copy_at-%s*interval '1 day' AND lower(m.body) LIKE q.pattern
                  ORDER BY m.sent_at DESC,m.id DESC LIMIT '''+str(MAX_CANDIDATES+1)+') o',
                [json.dumps([{'i':q['i'],'copy_at':q['at'].isoformat(),'pattern':_like(q['token'])} for q in pairs]),
                 BODY_READ,*msg_args,WINDOW_DAYS]):
                candidates.setdefault(r['i'],[]).append(r)
            for q in pairs:
                got=candidates.get(q['i'],[])
                if len(got)>MAX_CANDIDATES:continue  # a cap prevents establishing uniqueness: no hint
                same=[r for r in got if normalize(r['body'])==q['text'] and r['chat'] not in hidden
                    and r['chat']!=twins.get(q['chat'])]
                sources={r['chat'] for r in same}
                if len(sources)!=1:continue  # none, or a broadcast / canned reply
                source=same[0]
                if source['chat']==q['chat'] or source['chat_type']!='private':continue
                found.setdefault(q['chat'],{})[source['chat']]={'chat_id':source['chat'],'title':(source['title'] or '')[:TITLE],
                    'message_id':q['id'],'source_message_id':source['id'],'matched_at':q['at'],'heuristic':True}
    run.check()
    c.execute("SELECT set_config('statement_timeout',%s,true)",(previous,))
    result={}
    for chat,links in found.items():
        kept=_fit(sorted(links.values(),key=lambda x:(x['matched_at'],x['chat_id'])))
        if not kept:continue
        result.setdefault(chat,{})['related_chats']=kept
        for link in kept:
            result.setdefault(link['chat_id'],{}).setdefault('continued_by',[]).append({'chat_id':chat,
                'title':(fresh[chat]['title'] or '')[:TITLE],'message_id':link['message_id'],
                'source_message_id':link['source_message_id'],'matched_at':link['matched_at'],'heuristic':True})
    for value in result.values():
        if 'continued_by' in value:
            value['continued_by']=_fit(sorted(value['continued_by'],key=lambda x:(x['matched_at'],x['chat_id'])))
            if not value['continued_by']:del value['continued_by']
    return {chat:value for chat,value in result.items() if value}
