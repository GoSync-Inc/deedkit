"""Titles of private chats whose stored title is only the provider chat ID (ANYM-425).

Order for the peer: a non-raw incoming name in this chat, then the ANYM-279 sender-name
steps (a name seen in another readable chat, a visible non-conflicting PN/LID link) for
the chat's provider ID and for its latest incoming sender, then the phone number of a
`@c.us` chat. A `@lid` chat without a name keeps its stored title. Self chats are labelled
elsewhere (title_sql); names of the account's own messages are never a source.
"""
from .chat_queries import raw_title_sql,self_chat_sql
from .policy import scope_filter
from .sender_names import RAW_NAME_SQL,raw_name,resolve
from .whatsapp_identity import personal_id

IN_CHAT = 'latest_incoming_sender_name'
NAMED = ('seen_elsewhere','linked_identity')


def labels(c,p,pairs):
    """{(account, chat id): (title, basis)} for readable raw-titled private chats; basis is None when the stored title stays.

    Constant statements for any number of chats: one chat read and, only for WhatsApp
    identities, the sender-name statements of `resolve`. An ID `personal_id` rejects
    leaves the stored title.
    """
    pairs=sorted(set(pairs))
    if not pairs:return {}
    where,args=scope_filter(p,'c',chat_column='id')
    rows=c.execute('''SELECT c.account,c.id,c.provider_chat,c.title,peer.display_name AS name,
          (SELECT m.sender FROM messages m WHERE m.account=c.account AND m.chat=c.id AND m.direction='incoming'
            ORDER BY m.sent_at DESC,m.id DESC LIMIT 1) AS sender
        FROM unnest(%s::text[],%s::text[]) k(account,chat) JOIN chats c ON c.account=k.account AND c.id=k.chat
        LEFT JOIN LATERAL (SELECT sp.display_name FROM sender_profiles sp JOIN messages src ON src.id=sp.source_message
          WHERE sp.account=c.account AND sp.chat=c.id AND src.direction='incoming' AND NOT '''+RAW_NAME_SQL+'''
          ORDER BY sp.source_sent_at DESC,sp.id LIMIT 1) peer ON true
        WHERE '''+raw_title_sql()+' AND NOT '+self_chat_sql()+' AND '+where,
        [[a for a,_ in pairs],[x for _,x in pairs],*args]).fetchall()
    found={};peers=[]
    for row in rows:
        key=(row['account'],row['id'])
        if row['name'] and not raw_name(row['name'],row['provider_chat']):
            found[key]=(row['name'],IN_CHAT);continue
        primary=personal_id(row['provider_chat']);secondary=personal_id(row['sender'])
        if primary:peers.append({'account':row['account'],'chat':row['id'],'sender':row['provider_chat'],'_key':key,'_primary':True})
        if secondary and secondary!=primary:
            peers.append({'account':row['account'],'chat':row['id'],'sender':row['sender'],'_key':key,'_primary':False})
    if peers:
        resolve(c,p,peers,incoming=True)
        by_chat={}
        for peer in peers:by_chat.setdefault(peer['_key'],[]).append(peer)
        for key,candidates in by_chat.items():
            # Names before the phone number; the number only for the chat's own @c.us ID.
            named=next((x for basis in NAMED for x in candidates if x['sender_name_basis']==basis and x['sender_name']),None)
            phone=next((x for x in candidates if x['_primary'] and x['sender_name_basis']=='phone_number'),None)
            pick=named or phone
            if pick:found[key]=(pick['sender_name'],pick['sender_name_basis'])
    # Nothing found: the stored title, so a raw in-chat name that title_sql returned does not leak into message rows.
    for row in rows:found.setdefault((row['account'],row['id']),(row['title'],None))
    return found


def retitle(c,p,items,*,titles=None,account='account',chat='chat',title='chat_title',cap=None):
    """Set item[title] to the fallback title of its chat; `titles` reuses labels computed for the page."""
    if titles is None:
        titles=labels(c,p,[(i[account],i[chat]) for i in items if i.get(account) and i.get(chat)])
    for item in items:
        found=titles.get((item.get(account),item.get(chat)))
        if found:item[title]=found[0][:cap] if cap else found[0]
    return titles
