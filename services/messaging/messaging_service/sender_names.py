"""Readable sender labels; a raw provider ID stays in its own field, never the name.

Order: the name observed in the message's chat, a name of the same sender seen in
another readable chat, a name of a visible non-conflicting linked PN/LID identity,
the PN phone number. Each source authorizes exposure through a visible message.
"""
from .policy import scope_filter
from .whatsapp_identity import personal_id,visible_cte

# A stored name that only repeats the sender ID carries no name.
RAW_NAME_SQL = """(btrim(sp.display_name)='' OR btrim(sp.display_name) IN (sp.sender,
    split_part(sp.sender,'@',1),split_part(split_part(sp.sender,'@',1),':',1),
    '+'||split_part(split_part(sp.sender,'@',1),':',1)))"""


def raw_name(name, sender):
    if not isinstance(name,str) or not name.strip():return True
    if not isinstance(sender,str) or not sender:return False
    local=sender.split('@')[0];digits=local.split(':')[0]
    return name.strip() in (sender,local,digits,'+'+digits)


def _names(c,p,accounts,identities):
    """Newest visible, non-raw names per (account, normalized sender), newest first.

    No (account, sender) index exists: the split_part prefilter keeps the account scan
    cheap and the regex function only rechecks the few matches.
    """
    if not identities:return {}
    where,args=scope_filter(p,'m')
    rows=c.execute('''SELECT sp.account,sp.chat,sp.display_name,messenger_whatsapp_personal_id(sp.sender) AS identity
        FROM sender_profiles sp JOIN messages m ON m.id=sp.source_message
        WHERE '''+where+''' AND NOT m.deleted AND m.account=sp.account AND m.chat=sp.chat AND m.sender=sp.sender
          AND sp.account=ANY(%s) AND split_part(split_part(sp.sender,'@',1),':',1)||'@'||split_part(sp.sender,'@',2)=ANY(%s)
          AND messenger_whatsapp_personal_id(sp.sender)=ANY(%s) AND NOT '''+RAW_NAME_SQL+'''
        ORDER BY sp.source_sent_at DESC,sp.id''',[*args,sorted(accounts),sorted(identities),sorted(identities)]).fetchall()
    found={}
    for row in rows:found.setdefault((row['account'],row['identity']),[]).append(row)
    return found


def _links(c,p,accounts,identities):
    """Visible PN/LID partners; a conflict anywhere in the account blocks the link."""
    cte,args=visible_cte(p)
    rows=c.execute('WITH '+cte+''' SELECT DISTINCT v.account,v.pn,v.lid,v.verified FROM visible_whatsapp_identity v
        WHERE v.account=ANY(%s) AND (v.pn=ANY(%s) OR v.lid=ANY(%s))''',
        [*args,sorted(accounts),sorted(identities),sorted(identities)]).fetchall()
    partners={}
    for row in rows:
        for own,other in ((row['pn'],row['lid']),(row['lid'],row['pn'])):
            if own in identities:partners.setdefault((row['account'],own),set()).add(other if row['verified'] else None)
    return {key:next(iter(value)) for key,value in partners.items() if len(value)==1 and None not in value}


def resolve(c,p,rows,*,account='account',chat='chat',sender='sender',name='sender_name',basis='sender_name_basis'):
    """Fill rows in place. Telegram and other platforms only lose raw-ID names."""
    pending=[]
    for row in rows:
        row[basis]=None
        if raw_name(row.get(name),row.get(sender)):row[name]=None
        else:row[basis]='message';continue
        identity=personal_id(row.get(sender))
        if identity and row.get('platform') in (None,'whatsapp') and row.get(account):pending.append((row,identity))
    if not pending:return rows
    accounts={row[account] for row,_ in pending}
    seen=_names(c,p,accounts,{identity for _,identity in pending})
    unnamed=[(row,identity) for row,identity in pending
        if not any(n['chat']!=row.get(chat) for n in seen.get((row[account],identity),()))]
    links=_links(c,p,accounts,{identity for _,identity in unnamed}) if unnamed else {}
    linked=_names(c,p,accounts,set(links.values()))
    for row,identity in pending:
        other=next((n for n in seen.get((row[account],identity),()) if n['chat']!=row.get(chat)),None)
        partner=links.get((row[account],identity))
        if other:row.update({name:other['display_name'],basis:'seen_elsewhere'})
        elif partner and linked.get((row[account],partner)):
            row.update({name:linked[(row[account],partner)][0]['display_name'],basis:'linked_identity'})
        elif identity.endswith('@c.us'):row.update({name:'+'+identity.split('@')[0],basis:'phone_number'})
    return rows
