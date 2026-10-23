"""Owner-made person links (ANYM-283): explicit (account, sender) sets, never merged by name.

Writes run only on the owner connection (operator CLI): no operation permission, MCP tool,
REST route or gateway action. Reads expose a person only through members the key can read.
"""
import re
from uuid import UUID, uuid4

from .policy import scope_filter
from .search_query import fingerprint
from .whatsapp_identity import personal_id

UUID_TEXT = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')


def _identity(account, sender):
    for value in (account, sender):
        if not isinstance(value, str) or not 1 <= len(value) <= 256 or '\x00' in value:
            raise ValueError('invalid_identity')


def _person(value):
    if not isinstance(value, str) or not UUID_TEXT.fullmatch(value):
        raise ValueError('invalid_person')
    return str(UUID(value))


def whatsapp_siblings(c, account, sender):
    """The other half of a provider-observed, conflict-free PN/LID pair: a hint to link it too."""
    own = personal_id(sender)
    if not own:
        return []
    rows = c.execute('''SELECT DISTINCT CASE WHEN wi.pn=%(own)s THEN wi.lid ELSE wi.pn END AS sender,
          (SELECT person FROM person_links pl WHERE pl.account=wi.account
            AND pl.sender=CASE WHEN wi.pn=%(own)s THEN wi.lid ELSE wi.pn END) AS person
        FROM whatsapp_identity_observations wi WHERE wi.account=%(account)s AND %(own)s IN (wi.pn,wi.lid)
          AND NOT EXISTS(SELECT 1 FROM whatsapp_identity_observations other WHERE other.account=wi.account
            AND ((other.pn=wi.pn AND other.lid<>wi.lid) OR (other.lid=wi.lid AND other.pn<>wi.pn)))
        ORDER BY 1''', {'own': own, 'account': account}).fetchall()
    return [{'sender': r['sender'], 'person': str(r['person']) if r['person'] else None} for r in rows]


def _lock(c, person):
    """One lock per person serializes its existence check with every membership change (ANYM-283)."""
    c.execute("SELECT pg_advisory_xact_lock(hashtextextended('person_links:'||%s,0))", (person,))


def link(c, account, sender, person=None):
    _identity(account, sender)
    if person is not None:
        person = _person(person)
    c.execute("SET LOCAL transaction_timeout='5s'")
    if not c.execute('SELECT 1 FROM messages WHERE account=%s AND sender=%s AND NOT deleted LIMIT 1',
                     (account, sender)).fetchone():
        raise ValueError('unknown_identity')
    if person is not None:
        _lock(c, person)
        if not c.execute('SELECT 1 FROM person_links WHERE person=%s LIMIT 1', (person,)).fetchone():
            raise ValueError('unknown_person')
    target = person or str(uuid4())
    # The primary key decides between concurrent links of one identity.
    inserted = c.execute('''INSERT INTO person_links(person,account,sender) VALUES(%s,%s,%s)
        ON CONFLICT(account,sender) DO NOTHING RETURNING person''', (target, account, sender)).fetchone()
    if not inserted:
        current = c.execute('SELECT person FROM person_links WHERE account=%s AND sender=%s',
                            (account, sender)).fetchone()
        # Repeating the same link is a no-op; another person's identity is refused.
        if person is None or current is None or str(current['person']) != person:
            raise ValueError('identity_already_linked')
    return {'person': target, 'account': account, 'sender': sender, 'linked': bool(inserted),
            'whatsapp_siblings': whatsapp_siblings(c, account, sender)}


def unlink(c, account, sender):
    _identity(account, sender)
    c.execute("SET LOCAL transaction_timeout='5s'")
    # Lock the identity's person, then delete only under that person: if the identity moved to
    # another person meanwhile, follow it (a few times at most) instead of bypassing that lock.
    for _ in range(3):
        current = c.execute('SELECT person FROM person_links WHERE account=%s AND sender=%s',
                            (account, sender)).fetchone()
        if not current:
            raise ValueError('not_linked')
        person = str(current['person'])
        _lock(c, person)
        if c.execute('DELETE FROM person_links WHERE account=%s AND sender=%s AND person=%s RETURNING person',
                     (account, sender, person)).fetchone():
            return {'person': person, 'account': account, 'sender': sender}
    raise ValueError('link_changed')


def delete(c, person):
    person = _person(person)
    c.execute("SET LOCAL transaction_timeout='5s'")
    _lock(c, person)
    rows = c.execute('DELETE FROM person_links WHERE person=%s RETURNING account', (person,)).fetchall()
    if not rows:
        raise ValueError('unknown_person')
    return {'person': person, 'unlinked': len(rows)}


def list_people(c, person=None):
    if person is not None:
        person = _person(person)
    rows = c.execute('''SELECT person,account,sender,linked_at FROM person_links
        WHERE %s::uuid IS NULL OR person=%s::uuid ORDER BY person,account,sender''', (person, person)).fetchall()
    if person is not None and not rows:
        raise ValueError('unknown_person')
    people = {}
    for row in rows:
        people.setdefault(str(row['person']), []).append({'account': row['account'], 'sender': row['sender'],
            'linked_at': row['linked_at'].isoformat(),
            'whatsapp_siblings': whatsapp_siblings(c, row['account'], row['sender'])})
    return {'people': [{'person': key, 'members': members} for key, members in people.items()]}


def visible_members(c, p, person):
    """Members of a person the key can read now; none visible is the same not_found as unknown."""
    from .store import APIError
    try:
        person = _person(person)
    except ValueError:
        raise APIError('invalid_filter') from None
    where, args = scope_filter(p, 'm')
    rows = c.execute('''SELECT pl.account,pl.sender FROM person_links pl WHERE pl.person=%s AND EXISTS(
        SELECT 1 FROM messages m WHERE '''+where+''' AND NOT m.deleted AND m.account=pl.account AND m.sender=pl.sender)
        ORDER BY pl.account,pl.sender''', [person, *args]).fetchall()
    if not rows:
        raise APIError('not_found', 404)
    members = [[r['account'], r['sender']] for r in rows]
    return person, members, fingerprint(members)


def member_clause(alias, account_column, sender_column, members):
    """Exact (account, sender) pairs, never the cross-product of the two lists."""
    return ('('+alias+'.'+account_column+','+alias+'.'+sender_column+') IN (SELECT * FROM unnest(%s::text[],%s::text[]))',
            [[m[0] for m in members], [m[1] for m in members]])


def annotate(c, p, rows, *, sender='sender_id'):
    """Add `person` to rows of a linked identity, only where a member is visible to the key."""
    keys = sorted({(r['account'], r[sender]) for r in rows})
    if not keys:
        return
    where, args = scope_filter(p, 'm')
    found = {(r['account'], r['sender']): str(r['person']) for r in c.execute(
        '''SELECT pl.account,pl.sender,pl.person FROM person_links pl
        WHERE (pl.account,pl.sender) IN (SELECT * FROM unnest(%s::text[],%s::text[])) AND EXISTS(
          SELECT 1 FROM messages m WHERE '''+where+''' AND NOT m.deleted AND m.account=pl.account AND m.sender=pl.sender)''',
        [[k[0] for k in keys], [k[1] for k in keys], *args]).fetchall()}
    for row in rows:
        person = found.get((row['account'], row[sender]))
        if person:
            row['person'] = person
