"""Qualified conversation presentation; arrival order is never source chronology.

Provider text stays unchanged. Positions belong to this returned view, not a
persistent sequence, and a deterministic tie breaker does not prove phone order.
"""
from __future__ import annotations

import re

from .policy import scope_filter

KINDS=frozenset(('text','photo','video','voice','audio','document','sticker','location','contact','poll','system','unknown'))
LABELS={'photo':'Фото','video':'Видео','voice':'Голосовое','audio':'Аудио','document':'Документ',
    'sticker':'Стикер','location':'Местоположение','contact':'Контакт','poll':'Опрос',
    'system':'Служебное событие','unknown':'Сообщение неизвестного типа'}
# Every character str.splitlines breaks on: extracted text must not start a second line (ANYM-664).
_LINE_BREAKS=re.compile('[\\n\\r\\v\\f\\x1c-\\x1e\\x85\\u2028\\u2029]+')


def options(view='archive',include_system=False):
    from .store import APIError
    if view not in ('archive','conversation') or type(include_system) is not bool:
        raise APIError('invalid_filter')


def metadata_sql(alias='m',metadata='md'):
    # Synthetic and restored records may only carry immutable snapshot metadata.
    return f"coalesce({metadata}.metadata,(SELECT snapshot->'metadata' FROM message_revisions WHERE message={alias}.id AND revision={alias}.revision),'{{}}'::jsonb)"


def system_sql(alias='m',metadata='md'):
    meta=metadata_sql(alias,metadata)
    return f"coalesce({meta}->'is_service'='true'::jsonb,false) OR {meta}->>'message_kind'='system'"


def visible_sql(alias='m',metadata='md'):
    return 'NOT coalesce(('+system_sql(alias,metadata)+'),false)'


def meaningful_sql(alias='m',metadata='md'):
    meta=metadata_sql(alias,metadata)
    return f'''NOT {alias}.deleted AND {visible_sql(alias,metadata)}
        AND (length(btrim({alias}.body))>0 OR {meta}->>'media_kind' IS NOT NULL
          OR {meta}->>'message_kind' IN ('photo','video','voice','audio','document','sticker','location','contact','poll')
          OR EXISTS(SELECT 1 FROM media_jobs f WHERE f.message={alias}.id AND f.state<>'superseded')
          OR EXISTS(SELECT 1 FROM attachments f WHERE f.message={alias}.id AND f.active))'''


def numeric_order_sql(alias='m'):
    return f"CASE WHEN EXISTS(SELECT 1 FROM accounts source_account WHERE source_account.id={alias}.account AND source_account.platform='telegram') AND {alias}.provider_id ~ '^[0-9]{{1,18}}$' THEN {alias}.provider_id::bigint ELSE 0::bigint END"


def order_terms(alias='m'):
    return [alias+'.sent_at',numeric_order_sql(alias),alias+'.id']


def sort_key(row):
    provider=str(row.get('provider_id',''))
    number=int(provider) if row.get('platform')=='telegram' and provider.isascii() and provider.isdigit() and len(provider)<=18 else 0
    return row['sent_at'],number,row['id']


def contact_lines(block):
    """One line per shared contact (ANYM-529): name, first phone and its messenger, (+N) for more."""
    if not isinstance(block,dict) or not isinstance(block.get('contacts'),list):return []
    lines=[]
    for contact in block['contacts']:
        phones=contact.get('phones') or []
        line='[Контакт] '+(contact.get('name') or '')
        if phones:
            first=phones[0]
            line+=(': ' if contact.get('name') else '')+first['number']
            if first.get('whatsapp_id'):line+=' (WhatsApp)'
            elif contact.get('telegram_user_id'):line+=' (Telegram)'
            if len(phones)>1:line+=' (+'+str(len(phones)-1)+')'
        elif contact.get('telegram_user_id'):line+=' (Telegram)'
        lines.append(line)
    if lines and block.get('status')=='truncated':lines[-1]+=' [обрезано]'
    return lines


def excerpt_quote(excerpt):
    """The excerpt as one quoted line that cannot forge a label or end its quote early; '' when it has no words."""
    text=_LINE_BREAKS.sub(' ',excerpt.get('text') or '').replace('«','"').replace('»','"').strip()
    if not text:return ''
    return '«'+text+('…' if excerpt.get('complete') is False else '')+'»'


def _is_audio(file):
    # The mime rule of result_evidence._TRANSCRIPT.
    mime=file.get('mime') or ''
    return mime.startswith('audio/') or mime=='application/ogg'


def _text_reason(attachments):
    """Why a downloaded file's words are not in the message yet; an image description never becomes an excerpt."""
    pending=[f for f in attachments if f.get('extraction_status') in ('queued','leased','processing')
        or _is_audio(f) and f.get('enrichment_status') in ('queued','processing')]
    if pending:return 'расшифровка готовится' if any(_is_audio(f) for f in pending) else 'текст готовится'
    if any((f.get('text_excerpt') or {}).get('omitted')=='response_budget' for f in attachments):
        return 'текст в get_file_text'
    return None


def displayed_quote(row):
    """The quote present_message put into display_text, from the row's own marking and attachment."""
    for media in (row.get('display_text_media') or ())[:1]:
        for file in row.get('attachments') or ():
            if file.get('attachment_id')==media['attachment_id']:return excerpt_quote(file.get('text_excerpt') or {})
    return ''


def _split_display_text(row):
    """(the sender's text, label and buttons; where the quote sits; the quote)."""
    text=row['display_text']
    quote=displayed_quote(row)
    at=text.rfind(quote) if quote else -1
    if at<0:return text,len(text),''
    start=at-1 if at and text[at-1]==' ' else at
    return text[:start]+text[at+len(quote):],start,quote


def own_display_text(row):
    return _split_display_text(row)[0]


def bound_display_text(row,limit):
    """A limit bounds the sender's text and the label; the quote has its own bound and stays whole (ANYM-664)."""
    own,at,quote=_split_display_text(row)
    if len(own)<=limit:return row['display_text'],False
    own=own[:limit];at=min(at,limit)
    if not quote:return own,True
    return own[:at]+(' ' if at else '')+quote+own[at:],True


def cap_display_text(row,cap):
    if cap is None:return
    row['display_text'],cut=bound_display_text(row,cap)
    if cut:row['display_text_truncated']=True


def present_message(row,position):
    body=row.get('body',row.get('snippet','')) or ''
    observed=row.get('message_kind')
    if row.get('is_service') is True:kind='system'
    elif observed in KINDS:kind=observed
    elif row.get('media_kind') in KINDS:kind=row['media_kind']
    elif row.get('media_kind')=='animation':kind='video'
    elif row.get('provider_message_type') not in (None,'','chat','text'):kind='unknown'
    elif body:kind='text'
    elif row.get('attachments'):kind='document' if any(f.get('filename') for f in row['attachments']) else 'unknown'
    else:kind='unknown'
    row['kind']=kind;row['is_service']=kind=='system';row['position']=position
    row['conversation_view_version']=1
    label=LABELS.get(kind)
    if label:
        if kind=='document':
            name=next((f.get('filename') for f in row.get('attachments',[]) if f.get('filename')),None)
            if name:label+=': '+name[:256]
        duration=row.get('media_duration_seconds')
        if kind in ('voice','audio','video') and type(duration) in (int,float) and 0<=duration<=86400:
            seconds=int(duration);label+=' '+str(seconds//60)+':'+str(seconds%60).zfill(2)
        attachments=row.get('attachments',[])
        states={f.get('download_status') for f in attachments}
        if states and states!={'ready'}:
            reasons={f.get('download_reason') or f.get('error_code') for f in attachments}
            if reasons & {'paused_storage','disk_reserve'}:
                label+='; загрузка приостановлена: недостаточно места'
            elif reasons & {'outside_prefetch_window','outside_history_bounds'} and not states & {'processing','leased'}:
                label+='; файл доступен по запросу'
            elif states & {'processing','leased'}:
                label+='; файл загружается'
            elif states & {'queued'}:
                label+='; файл ожидает загрузки'
            elif states & {'deferred'}:
                label+='; файл доступен по запросу'
            else:label+='; файл пока недоступен'
        # ANYM-664: the first file with words; a sticker and a contact never have any, a video has no transcript profile.
        quote=''
        if kind not in ('sticker','contact'):
            for file in attachments:
                quote=excerpt_quote(file.get('text_excerpt') or {})
                if quote:
                    excerpt=file['text_excerpt']
                    row['display_text_media']=[{'attachment_id':file.get('attachment_id'),'source':excerpt['source'],
                        'model_generated':excerpt['model_generated'],'untrusted':True}]
                    break
            if not quote and states=={'ready'} and kind!='video':
                reason=_text_reason(attachments)
                if reason:label+='; '+reason
        lines=contact_lines(row.get('contact')) if kind=='contact' else []
        if lines:
            # A contact message has no caption: its body is the display name (native) or the raw
            # vCard (fallback), which stays in body; display shows only the structured lines.
            row['display_text']='\n'.join(lines)
        else:row['display_text']=(body+'\n' if body else '')+'['+label+']'+(' '+quote if quote else '')
    else:row['display_text']=body
    # ANYM-658: the transcript has only display_text; a keyboard shows its first labels, never a link or a kind.
    buttons=row.get('buttons')
    flat=[b for r in buttons.get('rows',[]) for b in r] if isinstance(buttons,dict) else buttons if isinstance(buttons,list) else []
    labels=[b['text'][:32] for b in flat[:8] if isinstance(b,dict) and b.get('text')]
    if labels and kind!='system':
        row['display_text']=(row['display_text']+'\n' if row['display_text'] else '')+'[Кнопки] '+' | '.join(labels)
    row['timestamp_precision']=row.get('timestamp_resolution') or 'unknown'
    row.setdefault('order_basis','timestamp_only');row.setdefault('order_status','timestamp_only')
    return row


def decorate_messages(c,p,rows,*,include_system=False):
    if not rows:return rows
    scope,args=scope_filter(p,'m')
    scope=scope.replace('m.','s.')
    ids=list(dict.fromkeys(r['id'] for r in rows))
    # All same-time siblings in the authorized chat qualify each result. A
    # sender/direction/search filter cannot make an ambiguous tie look certain.
    siblings=c.execute('''SELECT s.account,s.chat,s.sent_at,a.platform,count(*) AS n,
        bool_and(s.provider_id ~ '^[0-9]{1,18}$') AS numeric_ids
        FROM messages s JOIN accounts a ON a.id=s.account
        LEFT JOIN LATERAL (SELECT current.metadata FROM message_metadata current
          WHERE current.message=s.id AND current.revision=s.revision OFFSET 0) md ON true
        WHERE NOT s.deleted AND '''+scope+' AND '+('true' if include_system else visible_sql('s','md'))+'''
          AND (s.account,s.chat,s.sent_at) IN(SELECT account,chat,sent_at FROM messages WHERE id=ANY(%s))
        GROUP BY s.account,s.chat,s.sent_at,a.platform''',[*args,ids]).fetchall()
    ties={(r['account'],r['chat'],r['sent_at']):r for r in siblings}
    for position,row in enumerate(rows,1):
        tied=ties.get((row['account'],row['chat'],row['sent_at']))
        platform=(tied or {}).get('platform',row.get('platform'))
        provider=str(row.get('provider_id',''))
        numeric=platform=='telegram' and provider.isascii() and provider.isdigit() and len(provider)<=18
        certain=numeric and (tied is None or tied['numeric_ids'])
        row['order_basis']='provider_message_id' if certain else 'timestamp_only'
        row['order_status']='known' if certain else 'ambiguous' if tied and tied['n']>1 else 'timestamp_only'
        row['ordering']={'basis':row['order_basis'],'status':row['order_status'],
            'same_timestamp_messages':tied['n'] if tied else 1,'position_scope':'returned_view',
            'phone_order_verified':False}
        present_message(row,position)
    return rows


def truncation_warning(rows):
    truncated_ids=list(dict.fromkeys(r['id'] for r in rows
        if r.get('display_text_truncated') or r.get('body_truncated')))
    if truncated_ids:
        return {'code':'message_text_truncated',
            'message':'Текст сообщений обрезан; для подробностей используйте get_context(message_id, body_chars=20000).',
            'message_ids':truncated_ids}


def presentation(rows,*,order='asc',include_system=False):
    warning=truncation_warning(rows)
    warnings=[warning] if warning else []
    if any(r.get('order_status')=='ambiguous' for r in rows):
        warnings.append({'code':'ambiguous_timestamp_tie',
            'message':'Порядок сообщений с одинаковым временем неизвестен; позиции не доказывают порядок на телефоне.'})
    if order=='relevance':
        warnings.append({'code':'relevance_order','message':'Результаты отсортированы по совпадению, а не по порядку диалога.'})
    lines=[]
    mixed_scope=len({(r.get('account'),r.get('chat')) for r in rows})>1
    for row in rows:
        if row.get('kind')=='system':actor='Служебное событие'
        elif row.get('chat_type')=='group':actor='Вы → группа' if row.get('direction')=='outgoing' else 'Участник → группа'
        elif row.get('chat_type')=='channel':actor='Публикация канала'
        else:actor='Вы → собеседник' if row.get('direction')=='outgoing' else 'Собеседник → Вы'
        sender=row.get('sender_name') or row.get('username') or row.get('sender')
        stamp=row.get('sent_at');stamp=stamp.isoformat() if hasattr(stamp,'isoformat') else str(stamp or '')
        scope_label=(f" · аккаунт {row.get('account','')} · чат {row.get('chat_title') or row.get('chat','')} ({row.get('chat','')})" if mixed_scope else '')
        text=row['display_text']+(' […]' if row.get('display_text_truncated') or row.get('body_truncated') else '')
        if row.get('authorship')=='forwarded_by_sender':
            # Forwarded words are never the sender's own; name the stated origin or say it is unknown.
            author=((row.get('forward_origin') or {}).get('sender_name') or '')[:64]
            scope_label+=f' · переслано, автор: {author}' if author else ' · переслано, автор неизвестен'
        lines.append(f"{row['position']}. {actor}"+(f' ({sender})' if sender else '')+scope_label+f" · {stamp} · {row['id']}\n{text}")
    if warnings:lines.extend('⚠ '+warning['message'] for warning in warnings)
    return {'view':'conversation','contract_version':1,'order':order,
        'position_scope':'returned_view','system_filter':'included' if include_system else 'known_system_hidden',
        'warnings':warnings,'transcript':'\n\n'.join(lines),'content_is_untrusted':True}
