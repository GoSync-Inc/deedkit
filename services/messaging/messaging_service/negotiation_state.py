"""ANYM-893: one chat's scheduling signals (ANYM-892) fold into one negotiation and whose turn it is.

Pure: no database, network or clock. `evaluate(sources, now)` reads only its arguments, so the same source list and
`now` always give the same result. Versions and transitions belong to the store (ANYM-907), which compares results.

Vocabulary after iTIP (RFC 5546): REQUEST = proposed, COUNTER = counter, REPLY-accepted = agreed, REPLY-declined = a
decline that marks times declined, CANCEL = cancelled; DeedKit adds stale.

A source is a dict: message_id, revision, sender, is_owner, sent_at (aware datetime), chat_type ('private', 'group',
...), signal (`scheduling_rules.signals` output) and addressed (the message mentions or replies to the owner).
"""
import json
from datetime import datetime, timedelta

STATES = ('proposed', 'counter', 'agreed', 'cancelled', 'stale')
OPEN = ('proposed', 'counter')
OPENERS = ('propose', 'ask_availability')
MOVES = ('propose', 'reschedule', 'decline', 'ask_availability')
RANK = {'low': 0, 'medium': 1, 'high': 2}
STALE_AFTER = timedelta(days=7)
AGREED_KEEPS = timedelta(hours=24)
TIME_FIELDS = ('start', 'end', 'granularity', 'zone', 'zone_source', 'assumed')


def _at(value):return datetime.fromisoformat(value)


def _key(entry):return entry['start'], entry['end'], entry['granularity']


def _overlap(a, b):return _at(a['start']) < _at(b['end']) and _at(b['start']) < _at(a['end'])


def _entry(time, source, status):
    return {**{k: time.get(k) for k in TIME_FIELDS}, 'status': status, 'message_id': source['message_id'],
        'sender': source['sender']}


def _current(neg):return [t for t in neg['times'] if t['status'] == 'current']


def _content(source):
    """A total order on two copies of one message at one revision, so their input order never decides."""
    return json.dumps(source, sort_keys=True, default=str)


def _latest(sources):
    """Each message once, at its highest revision, ordered by (sent_at, message id)."""
    kept = {}
    for s in sources:
        old = kept.get(s['message_id'])
        if old is None or (s['revision'], _content(s)) > (old['revision'], _content(old)):kept[s['message_id']] = s
    return sorted(kept.values(), key=lambda s: (s['sent_at'], str(s['message_id'])))


def _open(source):
    neg = {'state': 'proposed', 'proposer': source['sender'], 'proposer_is_owner': bool(source['is_owner']),
        'times': [], 'declined_times': [], 'agreed_time': None, 'previous_agreed_time': None, 'ambiguous': False,
        'rescheduled': False, 'opened_at': source['sent_at'], 'sources': [], 'medium': 'unknown',
        'confidence': 'low', 'last': source, 'declined_last': False}
    _note(neg, source)
    _reject(neg, source)
    _add(neg, source)
    return neg


def _note(neg, source):
    signal = source['signal']
    neg['sources'].append(source['message_id'])
    neg['last'] = source
    if signal.get('medium', 'unknown') != 'unknown':neg['medium'] = signal['medium']
    if RANK.get(signal.get('confidence') or 'low', 0) > RANK[neg['confidence']]:neg['confidence'] = signal['confidence']


def _times(source, polarity):return [t for t in source['signal'].get('times') or [] if t.get('polarity') == polarity]


def _reject(neg, source):
    """A rejected time is never current: it declines the current (or agreed) times it overlaps, else it is kept
    in declined_times only."""
    for r in _times(source, 'rejected'):
        hits = [t for t in neg['times'] if t['status'] in ('current', 'agreed') and _overlap(t, r)]
        for t in hits:t['status'] = 'declined'
        if not hits and all(_key(d) != _key(r) for d in neg['declined_times']):
            neg['declined_times'].append(_entry(r, source, 'declined'))


def _add(neg, source):
    """The source's offered times join the current ones; an identical time from the same sender is not added twice."""
    for o in _times(source, 'offered'):
        if not any(t['status'] == 'current' and t['sender'] == source['sender'] and _key(t) == _key(o)
                   for t in neg['times']):
            neg['times'].append(_entry(o, source, 'current'))


def _counter(neg, source):
    for t in _current(neg):t['status'] = 'countered'
    neg.update(state='counter', proposer=source['sender'], proposer_is_owner=bool(source['is_owner']))
    _add(neg, source)


def _agree(neg, source, chosen, ambiguous):
    neg.update(state='agreed', agreed_time=chosen, ambiguous=ambiguous)
    for t in _current(neg):t['status'] = 'countered' if chosen is not None else 'current'
    if chosen is not None:
        match = [t for t in neg['times'] if _key(t) == _key(chosen)]
        if match:match[-1]['status'] = 'agreed'
        else:neg['times'].append(dict(chosen, status='agreed'))


def _inside(inner, outer):return _at(outer['start']) <= _at(inner['start']) and _at(inner['end']) <= _at(outer['end'])


def _accept(neg, source):
    if source['sender'] == neg['proposer']:
        _add(neg, source)
        return
    current, named = _current(neg), _times(source, 'offered')
    if named:
        # The named times select current options; only one selected option is an agreed time.
        selected = [c for c in current if any(_overlap(o, c) for o in named)]
        if not selected:
            # An "ok" naming a time nobody offered is a new offer, not an agreement.
            _counter(neg, source)
        elif len(selected) == 1:
            # One named time inside the option narrows it («ок, среда 10:00» inside «в среду утром»); a broader one
            # never widens it («ок, в пятницу» keeps the offered 12:00); two inside it («ок, в 12 или в 14») select
            # no single time, so the agreement stays ambiguous.
            option = selected[0]
            narrower = {_key(o): o for o in named if _inside(o, option) and _key(o) != _key(option)}
            if len(narrower) > 1:_agree(neg, source, None, True)
            else:_agree(neg, source, _entry(next(iter(narrower.values())), source, 'agreed') if narrower else option, False)
        else:
            _agree(neg, source, None, True)
    elif len(current) == 1:
        _agree(neg, source, current[0], False)
    else:
        _agree(neg, source, None, len(current) > 1)


def _after_agreed(neg, source, intent):
    if intent == 'accept' and neg['ambiguous']:
        # A later accept that names one of the options settles an ambiguous agreement.
        if _times(source, 'offered') and source['sender'] != neg['proposer']:_accept(neg, source)
        return
    if intent not in MOVES:return
    agreed = neg['agreed_time']
    # The agreed time and, after an ambiguous agreement, every option still open leave the table.
    for t in neg['times']:
        if t['status'] in ('agreed', 'current'):t['status'] = 'declined' if intent == 'decline' else 'countered'
    neg.update(state='counter', rescheduled=True, previous_agreed_time=agreed, agreed_time=None, ambiguous=False)
    # A bare decline leaves the proposer of the agreed time to offer again; any new time makes its sender the proposer.
    if intent != 'decline' or _times(source, 'offered'):
        neg.update(proposer=source['sender'], proposer_is_owner=bool(source['is_owner']))
    _add(neg, source)


def _step(neg, source):
    intent = source['signal'].get('intent', 'none')
    _note(neg, source)
    if intent == 'cancel':
        neg['state'] = 'cancelled'
        return
    _move(neg, source, intent)
    # A decline that leaves nothing on the table hands the turn to the proposer (criterion 2).
    neg['declined_last'] = (intent == 'decline' or bool(_times(source, 'rejected'))) and not _current(neg)


def _move(neg, source, intent):
    _reject(neg, source)
    if neg['state'] == 'agreed':
        _after_agreed(neg, source, intent)
        return
    if intent == 'accept':
        _accept(neg, source)
    elif intent in MOVES and _times(source, 'offered'):
        if source['sender'] != neg['proposer']:_counter(neg, source)
        else:_add(neg, source)
    elif intent == 'decline' and not _times(source, 'rejected') and source['sender'] != neg['proposer']:
        # "не могу" with no time declines everything on the table.
        for t in _current(neg):t['status'] = 'declined'


def _agreed_end(neg):
    """The agreed end; for an ambiguous agreement the latest option's end; None when no time is known at all."""
    agreed = neg['agreed_time']
    if agreed:return _at(agreed['end'])
    ends = [_at(t['end']) for t in _current(neg)]
    return max(ends) if ends else None


def _closed(neg, at):
    """Only after cancelled, stale, or agreed whose end is more than 24 h past does a new negotiation open. An
    agreement with no known time never counts as past."""
    if neg['state'] in ('cancelled', 'stale'):return True
    end = _agreed_end(neg) if neg['state'] == 'agreed' else None
    return end is not None and at - end > AGREED_KEEPS


def _stale(neg, at):
    current = _current(neg)
    return neg['state'] in OPEN and (at - neg['last']['sent_at'] > STALE_AFTER
                                     or bool(current) and all(_at(t['end']) <= at for t in current))


def _awaiting(neg):
    if neg['state'] not in OPEN:return 'none'
    last = neg['last']
    private = last.get('chat_type') == 'private'
    if neg['declined_last']:
        # The latest source declined what was left on the table: the proposer has to offer again.
        if neg['proposer_is_owner']:return 'owner'
        return 'peer' if private or last['is_owner'] else 'others'
    if last['is_owner']:return 'peer'
    if private or last.get('addressed'):return 'owner'
    return 'others'


def evaluate(sources, now):
    """The chat's negotiation from its current sources, or None when nothing in them is a negotiation."""
    neg = None
    for source in _latest(sources):
        signal = source.get('signal') or {}
        intent = signal.get('intent', 'none')
        if intent == 'none':continue
        at = source['sent_at']
        if neg is not None and _stale(neg, at):neg['state'] = 'stale'
        if neg is None or _closed(neg, at):
            if intent in OPENERS and RANK.get(signal.get('confidence') or 'low', 0) >= RANK['medium']:neg = _open(source)
            continue
        _step(neg, source)
    if neg is None:return None
    if _stale(neg, now):neg['state'] = 'stale'
    return {'state': neg['state'], 'awaiting': _awaiting(neg), 'proposer': neg['proposer'],
        'medium': neg['medium'], 'confidence': neg['confidence'], 'times': [dict(t) for t in neg['times']],
        'current_times': [dict(t) for t in _current(neg)], 'declined_times': [dict(t) for t in neg['declined_times']],
        'agreed_time': dict(neg['agreed_time']) if neg['agreed_time'] else None,
        'previous_agreed_time': dict(neg['previous_agreed_time']) if neg['previous_agreed_time'] else None,
        'ambiguous': neg['ambiguous'], 'rescheduled': neg['rescheduled'], 'opened_at': neg['opened_at'],
        'last_source_at': neg['last']['sent_at'], 'trigger_message': neg['last']['message_id'],
        'sources': list(neg['sources'])}
