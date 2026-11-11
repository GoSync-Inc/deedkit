"""Qualified conversation presentation; arrival order is never source chronology.

Provider text stays unchanged. Positions belong to this returned view, not a
persistent sequence, and a deterministic tie breaker does not prove phone order.
"""
from __future__ import annotations

import re

from .policy import scope_filter

KINDS=frozenset(('text','photo','video','voice','audio','document','sticker','location','contact','poll','system','group_change','call','event','unknown'))
LABELS={'photo':'Фото','video':'Видео','voice':'Голосовое','audio':'Аудио','document':'Документ',
    'sticker':'Стикер','location':'Местоположение','contact':'Контакт','poll':'Опрос',
    'system':'Служебное событие','group_change':'Изменение группы','call':'Звонок','event':'Событие','unknown':'Сообщение неизвестного типа'}
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
          OR {meta}->>'message_kind' IN ('photo','video','voice','audio','document','sticker','location','contact','poll','event')
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


def _preparing(file):
    """A downloaded file whose words may still come; an image's title and summary never become an excerpt (its visible text may, ANYM-987)."""
    return file.get('download_status')=='ready' and (file.get('extraction_status') in ('queued','leased','processing')
        or _is_audio(file) and file.get('enrichment_status') in ('queued','processing'))


def _text_reason(attachments,lost):
    pending=[f for f in attachments if _preparing(f) and f.get('attachment_id') not in lost]
    if pending:return 'расшифровка готовится' if any(_is_audio(f) for f in pending) else 'текст готовится'
    if any((f.get('text_excerpt') or {}).get('omitted')=='response_budget' for f in attachments):
        return 'текст в get_file_text'
    return None


def _button_line(row,kind):
    # ANYM-658: the transcript has only display_text; a keyboard shows its first labels, never a link or a kind.
    buttons=row.get('buttons')
    flat=[b for r in buttons.get('rows',[]) for b in r] if isinstance(buttons,dict) else buttons if isinstance(buttons,list) else []
    labels=[b['text'][:32] for b in flat[:8] if isinstance(b,dict) and b.get('text')]
    return '[Кнопки] '+' | '.join(labels) if labels and kind!='system' else ''


def displayed_quote(row):
    """The quote present_message put into display_text, from the row's own marking and first attachment."""
    if not row.get('display_text_media'):return ''
    return excerpt_quote((row.get('attachments') or [{}])[0].get('text_excerpt') or {})


def _split_display_text(row):
    """(the sender's text, label and buttons; where the quote sits; the quote) of a row not capped yet.

    The position comes from the row's structure: a caption or a button may hold the same words as the quote."""
    text=row['display_text']
    quote=displayed_quote(row)
    if not quote:return text,len(text),''
    line=_button_line(row,row.get('kind'))
    at=len(text)-(len(line)+1 if line else 0)-len(quote)
    return text[:at-1]+text[at+len(quote):],at-1,quote


def _own_text_cut(row,limit):
    """The truncation flags speak of the sender's text; a contact's lines are its text (ANYM-664 criterion 4)."""
    if row.get('kind')=='contact':return True
    return bool(row.get('body_truncated')) or len(row.get('body',row.get('snippet','')) or '')>limit


def bound_display_text(row,limit):
    """A limit bounds the sender's text and the label; the quote has its own bound and stays whole (ANYM-664)."""
    own,at,quote=_split_display_text(row)
    if len(own)<=limit:return row['display_text'],False
    own=own[:limit];at=min(at,limit)
    return own[:at]+(' ' if at else '')+quote+own[at:] if quote else own,_own_text_cut(row,limit)


def cap_display_text(row,cap):
    if cap is None:return
    row['display_text'],cut=bound_display_text(row,cap)
    if cut:row['display_text_truncated']=True


def present_message(row,position,*,lost=()):
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
    if (kind=='unknown' and row.get('platform')=='whatsapp'
            and row.get('provider_message_type') in ('placeholder','biz_content_placeholder')
            and not (body or row.get('attachments') or row.get('deleted'))):
        label='Содержимое недоступно на связанном устройстве WhatsApp. Откройте сообщение на основном телефоне'
    # ANYM-664: the first file's words; a sticker, a contact and a service event have none. A caption with a file but
    # no observed media kind stays kind text and gets the label a file without a caption gets.
    first=(row.get('attachments') or [{}])[0]
    quote=excerpt_quote(first.get('text_excerpt') or {}) if kind not in ('sticker','contact','system') else ''
    if quote:
        row['display_text_media']=[{'attachment_id':first.get('attachment_id'),'source':first['text_excerpt']['source'],
            'model_generated':first['text_excerpt']['model_generated'],'untrusted':True}]
        if kind=='text':label=LABELS['document']+(': '+first['filename'][:256] if first.get('filename') else '')
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
            elif reasons & {'outside_prefetch_window','outside_history_bounds','group_chat_on_request','channel_on_request'} and not states & {'processing','leased'}:
                label+='; файл доступен по запросу'
            elif states & {'processing','leased'}:
                label+='; файл загружается'
            elif states & {'queued'}:
                label+='; файл ожидает загрузки'
            elif states & {'deferred'}:
                label+='; файл доступен по запросу'
            else:label+='; файл пока недоступен'
        # A video has no transcript profile, a sticker no text to wait for.
        if not quote and kind not in ('sticker','contact','system','video'):
            reason=_text_reason(attachments,lost)
            if reason:label+='; '+reason
        lines=contact_lines(row.get('contact')) if kind=='contact' else []
        if lines:
            # A contact message has no caption: its body is the display name (native) or the raw
            # vCard (fallback), which stays in body; display shows only the structured lines.
            row['display_text']='\n'.join(lines)
        else:row['display_text']=(body+'\n' if body else '')+'['+label+']'+(' '+quote if quote else '')
    else:row['display_text']=body
    line=_button_line(row,kind)
    if line:row['display_text']=(row['display_text']+'\n' if row['display_text'] else '')+line
    row['timestamp_precision']=row.get('timestamp_resolution') or 'unknown'
    row.setdefault('order_basis','timestamp_only');row.setdefault('order_status','timestamp_only')
    return row


def decorate_messages(c,p,rows,*,include_system=False,groups=None):
    """groups (ANYM-889): rows split into results, each numbered from 1 as its own call; one tie read for all."""
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
    # A receipt says ready even when the original is gone with no eviction record: its label stays as it was.
    from .result_evidence import original_present
    lost={f['attachment_id'] for r in rows for f in r.get('attachments') or []
        if _preparing(f) and f.get('original_residency')!='evicted' and not original_present(f)}
    for position,row in ((position,row) for part in ([rows] if groups is None else groups) for position,row in enumerate(part,1)):
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
        present_message(row,position,lost=lost)
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
