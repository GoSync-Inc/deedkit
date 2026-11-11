"""WhatsApp event invitations (ANYM-924): the archived block, its response counts and the read-time calendar suggestion.

The block is what the sender stated (whatsmeow events.go): sender-written data, stored bounded; a join link is never
opened. The suggestion is only an offer an agent can put to the owner: nothing here or in its callers writes a
calendar (owner 11-09, ANYM-891).
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging
import math
import re

log = logging.getLogger(__name__)
EVENT_KEYS = frozenset(('name', 'description', 'start', 'end', 'location', 'join_link', 'canceled', 'is_call'))
PLACE_KEYS = ('title', 'address', 'latitude', 'longitude')
RESPONSE_KEYS = ('going', 'not_going', 'maybe')
TEXT_BOUNDS = {'name': 256, 'description': 2048, 'join_link': 2048, 'title': 256, 'address': 256}
MOMENT = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z')
# The calendar service's schedule call (services/calendar family_calendar/service/api.py Schedule): title 1-256
# characters, start ISO 8601 (with an offset: an instant), duration 5-1440 minutes (absent: its default of 30).
SCHEDULE_DURATION = (5, 1440)


def _text(value, key, *, required=False):
    if value is None and not required: return None
    if not isinstance(value, str) or '\x00' in value or len(value) > TEXT_BOUNDS[key]: raise ValueError('invalid_event')
    return value if value or required else None


def _moment(value):
    if value is None: return None
    if not isinstance(value, str) or not MOMENT.fullmatch(value): raise ValueError('invalid_event')
    try: datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ')
    except ValueError: raise ValueError('invalid_event') from None
    return value


def _coordinate(value, limit):
    # Type and range before any float conversion: a huge integer is out of range, NaN fails every comparison.
    if value is None: return None
    if type(value) not in (int, float) or not -limit <= value <= limit: raise ValueError('invalid_event')
    return round(float(value), 6)


def event_block(value):
    """{name, description, start, end, location: {title, address, latitude, longitude} | null, join_link, canceled,
    is_call}: name 256, description 2,048, title and address 256 characters; times UTC RFC 3339, an end never before
    its start."""
    if (not isinstance(value, dict) or set(value) != EVENT_KEYS or type(value['canceled']) is not bool
            or type(value['is_call']) is not bool):
        raise ValueError('invalid_event')
    place = value['location']
    if place is not None:
        if not isinstance(place, dict) or set(place) - set(PLACE_KEYS): raise ValueError('invalid_event')
        place = {'title': _text(place.get('title'), 'title'), 'address': _text(place.get('address'), 'address'),
                 'latitude': _coordinate(place.get('latitude'), 90), 'longitude': _coordinate(place.get('longitude'), 180)}
        if all(v is None for v in place.values()): place = None
    start, end = _moment(value['start']), _moment(value['end'])
    if start and end and end < start: raise ValueError('invalid_event')
    return {'name': _text(value['name'], 'name', required=True), 'description': _text(value['description'], 'description'),
            'start': start, 'end': end, 'location': place, 'join_link': _text(value['join_link'], 'join_link'),
            'canceled': value['canceled'], 'is_call': value['is_call']}


def event_responses(value):
    """{going, not_going, maybe}: how many answered each way, never who."""
    if (not isinstance(value, dict) or set(value) != set(RESPONSE_KEYS)
            or not all(type(value[k]) is int and 0 <= value[k] < 2**31 for k in RESPONSE_KEYS)):
        raise ValueError('invalid_event')
    return {k: value[k] for k in RESPONSE_KEYS}


def checked_event(data):
    """The event block and its counts are validated before the envelope is encoded or measured: one the archive cannot
    accept (or on a message that is no WhatsApp event) is dropped and only the reason code is logged, never its text;
    the message itself is still stored."""
    metadata = data.get('metadata')
    if not isinstance(metadata, dict) or (metadata.get('event') is None and metadata.get('event_responses') is None):
        return data
    metadata = dict(metadata)
    event = metadata.pop('event', None)
    responses = metadata.pop('event_responses', None)
    try:
        if metadata.get('message_kind') != 'event' or data.get('platform') != 'whatsapp' or event is None:
            raise ValueError('invalid_event')
        metadata['event'] = event_block(event)
        if responses is not None: metadata['event_responses'] = event_responses(responses)
    except (ValueError, TypeError, OverflowError):
        log.warning('event_dropped reason=invalid_event')
    return {**data, 'metadata': metadata}


def calendar_suggestion(event, message_id, now=None):
    """The offer an agent can put to the owner, in the schedule call's argument names (title, start, duration) plus
    what it shows alongside (end, location_text, description, source_message). None for a cancelled event, one with
    no name or start, or one that has already started."""
    if event['canceled'] or not event['name'] or not event['start']: return None
    start = datetime.fromisoformat(event['start'].replace('Z', '+00:00'))
    if start <= (now or datetime.now(timezone.utc)): return None
    duration = None
    if event['end']:
        minutes = math.ceil((datetime.fromisoformat(event['end'].replace('Z', '+00:00')) - start).total_seconds() / 60)
        if SCHEDULE_DURATION[0] <= minutes <= SCHEDULE_DURATION[1]: duration = minutes
    place = event['location'] or {}
    where = ', '.join(x for x in (place.get('title'), place.get('address')) if x) or None
    return {'title': event['name'], 'start': event['start'], 'duration': duration, 'end': event['end'],
            'location_text': where, 'description': event['description'], 'source_message': message_id}


def shape_event(row, now=None):
    """A read row's event in its full shape (a same-revision re-observation stores it with its null fields stripped),
    its counts and its suggestion. No event is no key."""
    event = row.pop('event', None)
    responses = row.pop('event_responses', None)
    if not isinstance(event, dict): return
    place = event.get('location')
    event = {'name': event.get('name') or '', 'description': event.get('description'), 'start': event.get('start'),
             'end': event.get('end'), 'location': {k: place.get(k) for k in PLACE_KEYS} if isinstance(place, dict) else None,
             'join_link': event.get('join_link'), 'canceled': event.get('canceled') is True, 'is_call': event.get('is_call') is True}
    row['event'] = event
    if isinstance(responses, dict): row['event_responses'] = {k: responses.get(k) or 0 for k in RESPONSE_KEYS}
    try: suggestion = calendar_suggestion(event, row.get('id'), now)
    except (TypeError, ValueError): suggestion = None
    if suggestion: row['calendar_suggestion'] = suggestion
