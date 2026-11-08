"""Readable sender labels; a raw provider ID stays in its own field, never the name.

Order: the name observed in the message's chat, a name of the same sender seen in
another readable chat, a name of a visible non-conflicting linked PN/LID identity,
the PN phone number. Each source authorizes exposure through a visible message.
"""
import re

from .policy import scope_filter
from .whatsapp_identity import personal_id

# A stored name that only repeats the sender ID carries no name: the ID itself, or
# its digits in any ID spelling (+digits, a device suffix, any @domain).
_ID_DIGITS = r'\+?([0-9]+)(?::[0-9]+)?(?:@[a-z.]+)?'
_TRIMMED = r"regexp_replace(sp.display_name,'^\s+|\s+$','','g')"
_DIGITS = "split_part(split_part(sp.sender,'@',1),':',1)"
NORMALIZED_SQL = _DIGITS+"||'@'||split_part(sp.sender,'@',2)"
RAW_NAME_SQL = ('('+_TRIMMED+"='' OR "+_TRIMMED+" IN (sp.sender,split_part(sp.sender,'@',1)) OR "+
    'coalesce(substring('+_TRIMMED+" from '^"+_ID_DIGITS+"$')="+_DIGITS+',false))')


def raw_name(name, sender):
    value=re.sub(r'^\s+|\s+$','',name) if isinstance(name,str) else ''
    if not value:return True
    if not isinstance(sender,str) or not sender:return False
    local=sender.partition('@')[0]
    match=re.fullmatch(_ID_DIGITS,value)
    return value in (sender,local) or bool(match and match[1]==local.split(':')[0])


def _names(c,p,accounts,identities,incoming=False):
    """Newest visible, non-raw names per (account, normalized sender), newest first.

    No (account, sender) index exists: the split_part prefilter keeps the account scan
    cheap and the regex function only rechecks the few matches. `incoming` keeps names
    observed on incoming messages only (a name on the account's own messages is the owner's).
    """
    if not identities:return {}
    where,args=scope_filter(p,'m')
    rows=c.execute('''SELECT sp.account,sp.chat,sp.display_name,messenger_whatsapp_personal_id(sp.sender) AS identity
        FROM sender_profiles sp JOIN messages m ON m.id=sp.source_message
        WHERE '''+where+(" AND m.direction='incoming'" if incoming else '')+''' AND NOT m.deleted AND m.account=sp.account AND m.chat=sp.chat AND m.sender=sp.sender
          AND sp.account=ANY(%s) AND '''+NORMALIZED_SQL+'''=ANY(%s)
          AND messenger_whatsapp_personal_id(sp.sender)=ANY(%s) AND NOT '''+RAW_NAME_SQL+'''
        ORDER BY sp.source_sent_at DESC,sp.id''',[*args,sorted(accounts),sorted(identities),sorted(identities)]).fetchall()
    found={}
    for row in rows:found.setdefault((row['account'],row['identity']),[]).append(row)
    return found


def _links(c,p,accounts,identities):
    """Visible PN/LID partners; a conflict anywhere in the account blocks the link.

    Distinct pairs are checked once, as in whatsapp_identity.resolve_alias, so
    repeated observations of one pair do not repeat the account-wide conflict test.
    """
    where,args=scope_filter(p,'m')
    rows=c.execute('''WITH candidate_pairs AS MATERIALIZED (
        SELECT DISTINCT account,pn,lid FROM whatsapp_identity_observations
        WHERE account=ANY(%s) AND (pn=ANY(%s) OR lid=ANY(%s)))
        SELECT pair.account,pair.pn,pair.lid,
          NOT EXISTS(SELECT 1 FROM whatsapp_identity_observations other
            WHERE other.account=pair.account AND other.pn=pair.pn AND other.lid<>pair.lid)
          AND NOT EXISTS(SELECT 1 FROM whatsapp_identity_observations other
            WHERE other.account=pair.account AND other.lid=pair.lid AND other.pn<>pair.pn) AS verified
        FROM candidate_pairs pair
        WHERE EXISTS(SELECT 1 FROM whatsapp_identity_observations wi JOIN messages m ON m.id=wi.source_message
          JOIN chats ic ON ic.id=m.chat
          WHERE wi.account=pair.account AND wi.pn=pair.pn AND wi.lid=pair.lid AND wi.account=m.account AND NOT m.deleted
            AND (wi.identity_id=m.sender OR wi.identity_id=ic.provider_chat) AND '''+where+')',
        [sorted(accounts),sorted(identities),sorted(identities),*args]).fetchall()
    partners={}
    for row in rows:
        for own,other in ((row['pn'],row['lid']),(row['lid'],row['pn'])):
            if own in identities:partners.setdefault((row['account'],own),set()).add(other if row['verified'] else None)
    return {key:next(iter(value)) for key,value in partners.items() if len(value)==1 and None not in value}


def resolve(c,p,rows,*,account='account',chat='chat',sender='sender',name='sender_name',basis='sender_name_basis',incoming=False):
    """Fill rows in place. Telegram and other platforms only lose raw-ID names."""
    pending=[]
    for row in rows:
        row[basis]=None
        if raw_name(row.get(name),row.get(sender)):row[name]=None
        else:row[basis]='message';continue
        identity=personal_id(row.get(sender))
        if identity and row.get(account):pending.append((row,identity))
    if pending:
        whatsapp={r['id'] for r in c.execute("SELECT id FROM accounts WHERE id=ANY(%s) AND platform='whatsapp'",
            (sorted({row[account] for row,_ in pending}),))}
        pending=[(row,identity) for row,identity in pending if row[account] in whatsapp]
    if not pending:return rows
    accounts={row[account] for row,_ in pending}
    seen=_names(c,p,accounts,{identity for _,identity in pending},incoming)
    unnamed=[(row,identity) for row,identity in pending
        if not any(n['chat']!=row.get(chat) for n in seen.get((row[account],identity),()))]
    links=_links(c,p,accounts,{identity for _,identity in unnamed}) if unnamed else {}
    linked=_names(c,p,accounts,set(links.values()),incoming)
    for row,identity in pending:
        other=next((n for n in seen.get((row[account],identity),()) if n['chat']!=row.get(chat)),None)
        partner=links.get((row[account],identity))
        if other:row.update({name:other['display_name'],basis:'seen_elsewhere'})
        elif partner and linked.get((row[account],partner)):
            row.update({name:linked[(row[account],partner)][0]['display_name'],basis:'linked_identity'})
        elif identity.endswith('@c.us'):row.update({name:'+'+identity.split('@')[0],basis:'phone_number'})
    return rows


def _elsewhere(c,p,row):
    """ANYM-831: the newest usable name, else username, of this Telegram sender in another chat of its account.

    One statement. A profile counts only through its own incoming, undeleted source message that
    the key can read (the checks of `_names`). There is no (account, sender) index: the account
    prefilter bounds the scan, as in `_names`.
    """
    where,args=scope_filter(p,'m')
    return c.execute('''SELECT CASE WHEN NOT '''+RAW_NAME_SQL+''' THEN sp.display_name END AS name,nullif(sp.username,'') AS username,
          to_char(sp.source_sent_at AT TIME ZONE 'UTC','YYYY-MM-DD') AS observed_on
        FROM sender_profiles sp JOIN messages m ON m.id=sp.source_message
        WHERE '''+where+''' AND m.account=sp.account AND m.chat=sp.chat AND m.sender=sp.sender AND NOT m.deleted
          AND m.direction='incoming' AND sp.account=%s AND sp.sender=%s AND sp.chat<>%s
          AND (NOT '''+RAW_NAME_SQL+''' OR nullif(sp.username,'') IS NOT NULL)
        ORDER BY (NOT '''+RAW_NAME_SQL+''') DESC,sp.source_sent_at DESC,sp.id LIMIT 1''',
        [*args,row['account'],row['sender'],row['chat']]).fetchone()


def anchor(c,p,row):
    """ANYM-831: the get_context anchor only, after `resolve`. Search and other rows keep the same-chat rule.

    An unnamed incoming Telegram sender takes `_elsewhere` with basis seen_elsewhere and the source
    date; an anchor still unnamed says not_observed, unless the chat is its own author (a channel
    post, an anonymous group admin).
    """
    if row.get('direction')!='incoming' or not row.get('sender') or row.get('sender_name') or row.get('username'):
        return row
    found=_elsewhere(c,p,row) if row.get('platform')=='telegram' else None
    if found:
        row.update(sender_name=found['name'],username=found['username'],sender_name_basis='seen_elsewhere',
            sender_name_observed_on=found['observed_on'])
    elif not (row.get('chat_type')=='channel' or row.get('chat_type')=='group' and row['sender']==row.get('provider_chat_id')):
        row['sender_name_unavailable']='not_observed'
    return row
