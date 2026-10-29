"""Stored classes of archive version conflicts (ANYM-557) and the one line an agent reads per account.

A class names the first field that differs, in the order archive.ingest tests them, between the conflict's
original candidate (its receipt envelope) and the revision it contradicted (`observed_revision`), never the
current retained revision. Only a class the reviewed table KNOWN lists is expected-unresolved with no action;
any other class, unclassifiable evidence and a NULL class (rows written by N-1 code) need attention.
"""
from datetime import datetime

from .envelope import validate

REASON_VERSION = 1
UNCLASSIFIED = 'unclassified'
PLATFORMS = {'telegram': 'Telegram', 'whatsapp': 'WhatsApp'}
FIELDS = {'sent_at': 'sent time differs', 'direction': 'direction differs', 'sender': 'sender differs',
          'body': 'text differs', 'media': 'attachments differ',
          'other': 'another field differs (formatting or deletion)'}
RELATIONS = {'equal_version': 'at the same provider version', 'older_version': 'in an older provider observation',
             'newer_version': 'in a newer provider observation',
             'unordered': 'with its order against later re-reads unknown'}
# Fields ingest compares at any provider version; only these can be stored without a provable order (ANYM-622).
IDENTITY = ('sent_at', 'direction', 'sender')
# Reviewed disposition (ANYM-530 research, owner Ready 10-26): a real contradiction at one provider version keeps the
# retained revision until later evidence arrives (archive.py, the resolve UPDATE), and no agent or owner step helps.
KNOWN = {
    'telegram_body_equal_version': 'Known Telegram case: two texts at one provider version; the archived revision stands until a later edit.',
    'telegram_direction_equal_version': 'Known Telegram case: the other direction at one provider version; the archived revision stands until later evidence.',
}
EXPLANATIONS = {
    **{f'{platform}_{field}_{relation}': f'{label} {text} {when}; inspect the chat.'
       for platform, label in PLATFORMS.items() for field, text in FIELDS.items()
       for relation, when in RELATIONS.items() if relation != 'unordered' or field in IDENTITY},
    **KNOWN,
    UNCLASSIFIED: 'The stored evidence of this conflict could not be classified; inspect the chat.',
}
LISTING_LIMIT = 20
TOP_CLASSES = 3
# The schema that stores classes. Before it (a rollback or an unmigrated test database) every conflict reads as
# unclassified and ingest writes the N-1 row, so no reader or collector depends on the new columns.
CLASS_SCHEMA = 54
CLASS_SQL = "coalesce(mc.reason,'unclassified')"


def stored(c):
    from .store import schema_version
    return schema_version(c) >= CLASS_SCHEMA


def class_sql(c):
    """SQL for a conflict's class: the stored one, a NULL one read as unclassified."""
    return CLASS_SQL if stored(c) else "'unclassified'"


def _time(value):
    if not isinstance(value, str): raise ValueError('time')
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None: raise ValueError('time')
    return parsed


# A revision snapshot is the validated envelope plus these archive fields; its `chat` is the archive chat ID.
SNAPSHOT_FIELDS = ('message', 'provider_chat', 'chat', 'revision')


def _classify(retained, candidate, retained_at=None):
    # Both sides pass the ingest validation, so every field is in its domain (direction is incoming or outgoing,
    # times carry a zone); anything else raises and is unclassified, never a known class (Astra r1 F2).
    candidate = validate(candidate)
    retained = validate({**{k: v for k, v in retained.items() if k not in SNAPSHOT_FIELDS},
                         'chat': retained['provider_chat']})
    platform = candidate['platform']
    if platform not in PLATFORMS or retained['platform'] != platform: return UNCLASSIFIED
    # archive.ingest: identity (sent_at, direction, sender) first, then content at one provider version.
    if _time(candidate['sent_at']) != _time(retained['sent_at']): field = 'sent_at'
    elif candidate['direction'] != retained['direction']: field = 'direction'
    elif candidate['sender'] != retained['sender']: field = 'sender'
    elif candidate['body'] != retained['body']: field = 'body'
    elif candidate['media'] != retained['media']: field = 'media'
    else: field = 'other'
    # The relation is to the version the archive compared at the conflict, not the snapshot's own (ANYM-561 s4).
    candidate_at = _time(candidate['updated_at'])
    if retained_at is None: retained_at = _time(retained['updated_at'])
    if not isinstance(retained_at, datetime) or retained_at.tzinfo is None: return UNCLASSIFIED
    relation = ('equal_version' if candidate_at == retained_at else
                'older_version' if candidate_at < retained_at else 'newer_version')
    return f'{platform}_{field}_{relation}'


def classify(retained, candidate, retained_at=None):
    """The class of one conflict: `retained` is the observed revision's snapshot, `candidate` the receipt envelope and
    `retained_at` the provider version the archive held then (ingest: exact).
    Never raises: ingest calls it inside its transaction, and bad evidence is a class of its own."""
    try:
        if not isinstance(retained, dict) or not isinstance(candidate, dict): return UNCLASSIFIED
        return _classify(retained, candidate, retained_at)
    except Exception:
        return UNCLASSIFIED


def reclassify(row):
    """ANYM-622: the class of an open conflict stored without one, from the runner's evidence of one row (`envelope`,
    `observed`, `archive_version`: the message's provider version now, `tombstoned`). Content conflicts are raised
    only at the archive's version (archive.ingest_outcome), so their relation is `equal_version`. An identity conflict
    is raised at any version: its relation is exact only when the version now equals the observed revision's, since the
    stored version never goes back (a revision is never stale; an unchanged re-read takes greatest) and so it was that
    one at the conflict too, and no provider tombstone rewrote the candidate; otherwise `unordered`. A row migration 017
    reconstructed (its observed_at is the receipt's received_at, chat_evidence.backfill) names its revision by
    transaction start times, which need not follow the account lock: on a message with more than one revision that
    revision may not be the contradicted one, so it is `unclassified` (Astra r1 on #454). Never raises."""
    try:
        envelope, observed = row['envelope'], row['observed']
        if not isinstance(envelope, dict) or not isinstance(observed, dict): return UNCLASSIFIED
        if row['reconstructed'] is not False: return UNCLASSIFIED
        name = classify(observed, envelope, _time(envelope['updated_at']))
        if name == UNCLASSIFIED: return name
        platform, field = name[:-len('_equal_version')].split('_', 1)
        if field not in IDENTITY: return name
        version = row['archive_version']
        if (row['tombstoned'] is not False or not isinstance(version, datetime)
                or version != _time(observed['updated_at'])): return f'{platform}_{field}_unordered'
        return classify(observed, envelope)
    except Exception:
        return UNCLASSIFIED


def known_classes():
    return sorted(KNOWN)


def explanation(name):
    return EXPLANATIONS.get(name, EXPLANATIONS[UNCLASSIFIED])


def report(counts):
    """The account or chat line from {class: open conflicts}: attention, known, top classes and one step."""
    known = sum(n for name, n in counts.items() if name in KNOWN)
    attention = sum(counts.values()) - known
    top = sorted((item for item in counts.items() if item[1]), key=lambda item: (-item[1], item[0] in KNOWN, item[0]))
    return {'attention': attention, 'known': known,
            'top_classes': [{'class': name, 'count': n, 'explanation': explanation(name)} for name, n in top[:TOP_CLASSES]],
            'next_step': 'inspect_chat' if attention else 'none_required'}


def message_conflict(name):
    """What a message with an open conflict carries: its class and the step, never a tool name."""
    return {'class': name, 'next_step': 'none_required' if name in KNOWN else 'inspect_chat'}


def grouped(c, column, values):
    """{account or chat: (open by class, active, historical)}, counted per stored class. `column` is a constant."""
    if column not in ('account', 'chat'): raise ValueError('invalid_conflict_group')
    result = {}
    for row in c.execute(f'''SELECT m.{column} AS key,{class_sql(c)} AS class,
            count(*) FILTER(WHERE mc.resolved_at IS NULL) AS active,
            count(*) FILTER(WHERE mc.resolved_at IS NOT NULL) AS historical
            FROM message_conflicts mc JOIN messages m ON m.id=mc.message
            WHERE m.{column}=ANY(%s) GROUP BY 1,2''', (list(values),)):
        open_by_class, active, historical = result.get(row['key'], ({}, 0, 0))
        if row['active']: open_by_class[row['class']] = row['active']
        result[row['key']] = (open_by_class, active + row['active'], historical + row['historical'])
    return result


def listing(c, chat):
    """At most LISTING_LIMIT open conflicts of one permitted chat, attention first; no message content."""
    name = class_sql(c)
    rows = c.execute(f'''SELECT mc.message AS message_id,{name} AS class,mc.observed_at
        FROM message_conflicts mc JOIN messages m ON m.id=mc.message
        WHERE m.chat=%s AND mc.resolved_at IS NULL
        ORDER BY {name}=ANY(%s),mc.observed_at DESC,mc.receipt_digest LIMIT %s''',
        (chat, known_classes(), LISTING_LIMIT)).fetchall()
    return [{'message_id': r['message_id'], 'class': r['class'], 'explanation': explanation(r['class']),
             'observed_at': r['observed_at']} for r in rows]


# ANYM-616: what a known class means for a quote of the archived message a conversation block shows.
SHOWN = {
    'telegram_body_equal_version': 'Two texts at one provider version; the archived text is shown and the provider may show the other now, so caveat a quote.',
    'telegram_direction_equal_version': 'Two directions at one provider version; the archived direction is shown and the provider may show the other (and its text) now, so caveat a quote.',
}


def known_conflict(evidence):
    """{class, meaning} when a message's open conflicts are all of a KNOWN class, else None.

    result_evidence names one class per message, a class that needs attention first, so a known class there means
    every open conflict of the message is known."""
    if not isinstance(evidence, dict) or not evidence.get('active_conflicts'): return None
    conflict = evidence.get('conflict')
    name = conflict.get('class') if isinstance(conflict, dict) else None
    return {'class': name, 'meaning': SHOWN[name]} if isinstance(name, str) and name in KNOWN and name in SHOWN else None
