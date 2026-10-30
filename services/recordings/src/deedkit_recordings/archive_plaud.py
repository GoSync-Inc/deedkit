"""Verified bounded whole-text observations over the pinned personal PLAUD MCP."""
from . import plaud
from .archive_store import MAX_TEXT
from .security import Failure

MAX_SEGMENTS = 10000


async def transcript(call, recording_id):
    async def observation():
        position, cursors, rows, total = None, set(), [], None
        size = 0
        for _ in range(MAX_SEGMENTS // 500 + 1):
            args = {'file_id':recording_id, 'block':'transaction', 'limit':500}
            if position is not None:
                args['cursor'] = position
            value = await call('get_transcript', args)
            if value == [] and not rows:
                return 'unavailable', '', 0
            if not isinstance(value,dict) or value.get('file_id') != recording_id or value.get('block') != 'transaction':
                raise Failure('invalid_provider_response')
            if any(type(value.get(k)) is not int for k in ('total','offset','returned','limit')):
                raise Failure('invalid_provider_response')
            count = value['total']
            if not 0 <= count <= MAX_SEGMENTS:
                raise Failure('archive_content_limit')
            segments = value.get('segments')
            if (not isinstance(segments,list) or len(segments)>500 or value['limit']!=500
                    or value['returned']!=len(segments) or value['offset']!=len(rows)
                    or value['offset']+len(segments)>count or (total is not None and total!=count)):
                raise Failure('invalid_provider_response')
            total = count
            # Reuse the existing whitelist and scalar validation without changing
            # the on-demand adapter's deliberate single-window failure contract.
            single = {**value,'offset':0,'total':len(segments),'next_cursor':None}
            _, text = plaud.content(single,'transcript',recording_id)
            size += len(text.encode()) + (1 if rows and text else 0)
            if size > MAX_TEXT:
                raise Failure('archive_content_limit')
            rows.extend(text.split('\n') if text else [])
            following = value.get('next_cursor')
            if following is None:
                if len(rows)!=total:
                    raise Failure('invalid_provider_response')
                return ('available' if rows else 'empty'), '\n'.join(rows), total
            if (not segments or len(rows)>=total or not isinstance(following,str)
                    or not 1<=len(following)<=2048 or not following.isascii()
                    or any(ord(c)<33 or ord(c)>126 for c in following) or following in cursors):
                raise Failure('invalid_provider_response')
            cursors.add(following)
            position = following
        raise Failure('archive_content_limit')

    first = await observation()
    if first[0] == 'unavailable':
        return first[:2]
    second = await observation()
    if first != second:
        raise Failure('source_changed',409)
    return first[:2]
