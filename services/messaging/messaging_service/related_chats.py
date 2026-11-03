"""Heuristic hint on chat rows: which older private chat a brand-new private chat probably continues (ANYM-656).

The only basis is a copy: one of a new chat's first incoming messages repeats, after normalization, an owner message
sent in exactly one other private chat in the 7 days before it. Read-only: no schema, stored link or event. Everything
runs over the caller's visible corpus (`scope_filter`), inside one savepoint under one elapsed-time deadline; any error
or timeout drops every partial hint and the caller returns its ordinary rows.

A hint is made only when the bounded lookup can establish it. The candidate lookup is a `lower(body) LIKE` token match on
the existing trigram index, so the equality it can see is exactly "same text after lowercasing and whitespace collapse":
no Unicode compatibility folding (a lookup that cannot see a variant could not exclude it as a broadcast), a token the
database cannot lowercase like Python is not used, and a body that is too long to compare whole is left out.
"""
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from .policy import scope_filter

log=logging.getLogger('messaging_service.related_chats')
NEW_DAYS=7  # a chat is new while its first archived message (by message time) is this young
WINDOW_DAYS=7  # the owner message was sent at most this long before its copy
MIN_CHARS=24
MAX_CHECKED=5  # new chats checked per page
FIRST_INCOMING=3
MAX_CANDIDATES=200  # outgoing candidates per copy; more means uniqueness cannot be established
MAX_BYTES=2048  # a body whose normalized text is longer is not compared at all (never a truncated prefix)
BODY_READ=8192  # characters read per body; a longer raw body cannot be compared whole
DEADLINE_SECONDS=1.0
MAX_RELATED=3
TITLE=60
LIST_CHARS=600
KEYS=('related_chats','continued_by')


class Deadline(Exception):
    """The helper's one elapsed-time budget is spent."""


def normalize(text):
    """Lowercased, whitespace collapsed. Nothing is cut: a body too long to compare whole is left out by `usable`."""
    return ' '.join((text or '').lower().split())


def usable(text):
    # Under 24 characters or a single token (a bare URL, number or address) proves nothing; over 2 KB is not compared.
    return len(text)>=MIN_CHARS and ' ' in text and len(text.encode())<=MAX_BYTES


def _like(token):
    return '%'+token.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'


def _token(raw):
    """The longest token of a raw body (by lowered length) that lowercases the same in any locale rule: else ''."""
    best=''
    for word in raw.split():
        folded=word.lower()
        if word.upper().lower()==folded and len(folded)>len(best.lower()):best=word
    return best


class _Run:
    def __init__(self,c):self.c=c;self.end=time.monotonic()+DEADLINE_SECONDS

    def check(self):
        if self.end-time.monotonic()<=0:raise Deadline('budget_exhausted')

    def arm(self):
        # Each statement gets what is left of the one budget as its local statement timeout.
        self.check()
        left=max(1,int((self.end-time.monotonic())*1000))
        self.c.execute("SELECT set_config('statement_timeout',%s,true)",(str(left)+'ms',))

    def rows(self,step,sql,args):
        self.arm()
        return self.c.execute('/* related_chats:'+step+' */ '+sql,args).fetchall()


def _fit(entries):
    # At most 3 entries and 600 characters; a list that cannot fit even one entry is dropped.
    entries=entries[:MAX_RELATED]
    while entries and len(json.dumps(entries,ensure_ascii=False,separators=(',',':'),default=str))>LIST_CHARS:entries.pop()
    return entries


def hints(c,p,entries):
    """{chat id: {'related_chats': [...], 'continued_by': [...]}} for the page's rows; {} on any failure.

    entries: [{'id','type','last_activity'}] in page order. Independent of the page's own filters: aliases and every
    candidate come from the principal's whole visible corpus.
    """
    floor=datetime.now(timezone.utc)-timedelta(days=NEW_DAYS)
    # A chat whose newest message is older than the window cannot have a first message inside it.
    todo=[e for e in entries if e['type']=='private' and (e.get('last_activity') is None or e['last_activity']>=floor)]
    if not todo:return {}
    try:
        with c.transaction():  # a savepoint: a failed statement leaves the caller's transaction usable
            return _find(c,p,todo,floor)
    except Exception as failure:
        log.warning('related_chats_abandoned %s',type(failure).__name__)
        return {}


def _aliases(run,p):
    """(alias chats, twin map) over the principal's whole visible corpus: the LID chat of each verified readable pair."""
    if not run.rows('gate',"SELECT EXISTS(SELECT 1 FROM accounts WHERE id=ANY(%s) AND platform='whatsapp') AS x",
            [p['accounts']])[0]['x']:return set(),{}
    from .whatsapp_identity import chat_links
    run.arm()
    links=[link for link in chat_links(run.c,p,None,paired_only=True) if link['pn_chat']]
    twins={}
    for link in links:twins[link['lid_chat']]=link['pn_chat'];twins[link['pn_chat']]=link['lid_chat']
    return {link['lid_chat'] for link in links},twins


def _find(c,p,todo,floor):
    run=_Run(c)
    previous=c.execute("SELECT current_setting('statement_timeout') AS v").fetchone()['v']
    chat_scope,chat_args=scope_filter(p,'c',chat_column='id')
    # The first message per direction reads message_reply_order (a bounded indexed walk from the chat's oldest message);
    # one ORDER BY over both directions would let the planner walk message_live_date across the whole archive.
    fresh={r['id']:r for r in run.rows('new','''SELECT c.id,c.title FROM chats c CROSS JOIN LATERAL (
        SELECT least((SELECT m.sent_at FROM messages m WHERE m.account=c.account AND m.chat=c.id AND m.direction='incoming'
            AND NOT m.deleted AND NOT m.purged ORDER BY m.sent_at LIMIT 1),
          (SELECT m.sent_at FROM messages m WHERE m.account=c.account AND m.chat=c.id AND m.direction='outgoing'
            AND NOT m.deleted AND NOT m.purged ORDER BY m.sent_at LIMIT 1)) AS sent_at) f
        WHERE c.id=ANY(%s) AND c.chat_type='private' AND c.purged_at IS NULL AND '''+chat_scope+' AND f.sent_at>=%s',
        [[e['id'] for e in todo],*chat_args,floor])}
    new=[e['id'] for e in todo if e['id'] in fresh][:MAX_CHECKED]
    found={}
    if new:
        msg_scope,msg_args=scope_filter(p,'m')
        pairs=[]
        for r in run.rows('first','''SELECT n.chat,f.id,f.sent_at,left(f.body,%s) AS body,char_length(f.body)>%s AS long
            FROM unnest(%s::text[]) AS n(chat) JOIN chats c ON c.id=n.chat CROSS JOIN LATERAL (
              SELECT m.id,m.sent_at,m.body FROM messages m
              WHERE m.account=c.account AND m.chat=c.id AND m.direction='incoming' AND NOT m.deleted AND NOT m.purged
                AND '''+msg_scope+' ORDER BY m.sent_at,m.id LIMIT '+str(FIRST_INCOMING)+') f',[BODY_READ,BODY_READ,new,*msg_args]):
            text=normalize(r['body'])
            raw=_token(r['body'] or '')
            # The trigram index needs 3 characters; a body without a usable token cannot be checked cheaply.
            if not r['long'] and usable(text) and len(raw)>=3:
                pairs.append({'i':len(pairs),'chat':r['chat'],'id':r['id'],'at':r['sent_at'],'text':text,'raw':raw})
        if pairs:
            # The database must lowercase the token as Python does, in both cases of the source text.
            bad={r['i'] for r in run.rows('fold','''SELECT q.i FROM jsonb_to_recordset(%s::jsonb) AS q(i int,raw text,up text,folded text)
                WHERE lower(q.raw)<>q.folded OR lower(q.up)<>q.folded''',
                [json.dumps([{'i':q['i'],'raw':q['raw'],'up':q['raw'].upper(),'folded':q['raw'].lower()} for q in pairs])])}
            pairs=[q for q in pairs if q['i'] not in bad]
        if pairs:
            # Alias chats and a new chat's own twin leave the lookup in SQL, before the cap counts anything.
            aliases,twins=_aliases(run,p)
            candidates={}
            for r in run.rows('candidates','''SELECT q.i,o.* FROM jsonb_to_recordset(%s::jsonb) AS q(i int,copy_at timestamptz,pattern text,skip jsonb)
                CROSS JOIN LATERAL (SELECT m.id,m.chat,m.sent_at,left(m.body,%s) AS body,char_length(m.body)>%s AS long,
                    c.title,c.chat_type
                  FROM messages m JOIN chats c ON c.id=m.chat AND c.purged_at IS NULL
                  WHERE '''+msg_scope+''' AND m.direction='outgoing' AND NOT m.deleted AND NOT m.purged
                    AND m.chat NOT IN (SELECT jsonb_array_elements_text(q.skip))
                    AND m.sent_at<=q.copy_at AND m.sent_at>=q.copy_at-%s*interval '1 day' AND lower(m.body) LIKE q.pattern
                  ORDER BY m.sent_at DESC,m.id DESC LIMIT '''+str(MAX_CANDIDATES+1)+') o',
                [json.dumps([{'i':q['i'],'copy_at':q['at'].isoformat(),'pattern':_like(q['raw'].lower()),
                    'skip':sorted(aliases|{twins[q['chat']]}) if q['chat'] in twins else sorted(aliases)} for q in pairs]),
                 BODY_READ,BODY_READ,*msg_args,WINDOW_DAYS]):
                candidates.setdefault(r['i'],[]).append(r)
            for q in pairs:
                got=candidates.get(q['i'],[])
                # A cap, or a body too long to compare whole, prevents establishing uniqueness: no hint.
                if len(got)>MAX_CANDIDATES or any(r['long'] for r in got):continue
                same=[r for r in got if normalize(r['body'])==q['text']]
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
