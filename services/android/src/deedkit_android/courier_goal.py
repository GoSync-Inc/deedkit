"""Courier chat autonomy (ANYM-978): the owner's goal for an order, the bounds of a series of replies, the handover.

One rule per principal, app and order (or one `standing` rule that opens an order's rule on first use) says that the
owner gave the agent a goal for that order's courier. `reply` may then send logistics messages to that courier without
a yes per message, inside these bounds: a message cap that counts every attempt (reserved before the tap), a rate
limit, a text gate, a report-to-the-owner lag and the failed-turn limit. Whatever the code cannot judge safe ends the
rule as `handed_over`: the chat goes back to the owner with a summary, and the first tool's preview and send (ANYM-975,
the owner's yes per message) stay the only way to talk to that courier. Nothing here taps or types: it is state and
text rules; courier_chat.CourierChat does the device work.
"""

import hashlib
import json
import math
import re
import time

from .config import Refused

DEFAULT_MESSAGES, MAX_MESSAGES = 6, 12
DEFAULT_TTL_MIN, DEFAULT_STANDING_TTL_MIN = 180, 1440
MIN_INTERVAL_S = 30
MAX_ACTIVE_ORDERS = 5
FAILED_TURNS = 3
UNREPORTED = 3
TEXT_CHARS = 300
LOG_ROWS, SEEN_ROWS = 80, 120
STANDING = '*'
STORE_ACTIONS = frozenset({'goal', 'handover', 'summary'})
clock = time.time

# Whole words, and prefixes of 5+ letters; a short word never matches as a prefix (`pintu` is a door, `pin` is not).
EXACT = {
    'money': {'tip', 'tips', 'tipping', 'pay', 'paid', 'paying', 'cash', 'uang', 'duit', 'fee', 'fare', 'price',
              'harga', 'rp', 'idr', 'usd', 'ongkir', 'biaya', 'transfer', 'dollar', 'dollars', 'rupiah', 'ribu',
              'rb', 'jt', 'juta', 'cod'},
    'cancel': {'cancel', 'cancelled', 'canceled', 'batal'},
    'refund': {'refund'},
    'complaint': {'complain', 'rating', 'bintang', 'review', 'ulasan', 'scam', 'report'},
    'secret': {'pin', 'otp', 'cvv', 'cvc', 'password', 'sandi', 'passcode', 'kode', 'code'},
    'hostile': {'anjing', 'bangsat', 'kontol', 'tolol', 'bodoh', 'goblok', 'brengsek', 'sialan', 'idiot', 'stupid',
                'fuck', 'shit', 'damn', 'asshole', 'bastard', 'bitch'},
}
PREFIX = {
    'money': ('payment', 'pembayaran', 'bayar', 'charge', 'чаев', 'оплат', 'деньг', 'цен', 'плат'),
    'cancel': ('cancel', 'batalkan', 'dibatalkan', 'отмен'),
    'refund': ('refund', 'возврат', 'pengembalian'),
    'complaint': ('complain', 'komplain', 'lapor', 'жалоб', 'penipu'),
    'secret': ('verification', 'verifikasi', 'kartu', 'card', 'credential'),
    'hostile': ('ancam', 'threat', 'polisi', 'police', 'полици'),
}
WORD = re.compile(r'[^\W_]+')
LINK = re.compile(r'https?://|www\.|@|\b\w+\.(?:com|net|org|id|io|co|me)\b')
AMOUNT = re.compile(r'(?:rp|idr|usd|\$|€|₽)\s*\d|\d\s*(?:k|rb|ribu|jt|juta)\b')
DIGITS = re.compile(r'\d{7,}')


def words(text):
    return ' '.join(str(text or '').casefold().split())


def topic(text, inbound=False):
    """The first topic the text names, else None. Outbound text also fails on length, links and long numbers."""
    said = words(text)
    if not inbound and len(str(text or '')) > TEXT_CHARS:
        return 'length'
    for word in WORD.findall(said):
        for name, found in EXACT.items():
            if word in found:
                return name
        for name, stems in PREFIX.items():
            if word.startswith(stems):
                return name
    if AMOUNT.search(said):
        return 'money'
    if not inbound:
        if LINK.search(said):
            return 'link'
        if DIGITS.search(re.sub(r'(?<=\d)[ .\-](?=\d)', '', said)):
            return 'number'
    return None


def rows_key(row):
    return hashlib.sha1(words(row).encode()).hexdigest()[:12]


class Gate(Refused):
    """A refused or ended turn that names the next call; `observe=None`: no device was touched, so no screenshot."""

    def __init__(self, code, args=None, status=409, observe=None, **more):
        super().__init__(code, status)
        self.observe = observe
        self.extra = dict(more)
        if args is not None:
            self.extra.setdefault('next_action', 'propose_to_the_owner_with_preview_then_send')
            self.extra['next_call'] = {'tool': 'android_courier_chat', 'caller_supplies': ['session_id', 'request_id', 'message'],
                                       'arguments': {'action': 'preview', 'device_id': args['device_id'],
                                                     'app': args['app'], 'order': args['order']}}


class Handover(Gate):
    """The rule ended on this turn: `block` goes to the owner (reason, summary, chat)."""

    def __init__(self, reason, block, observe=None):
        super().__init__(reason, observe=observe)
        self.block = block


class Goals:
    def __init__(self, db):
        self.db = db
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS courier_goals (
                owner TEXT, app TEXT, key TEXT, label TEXT, goal TEXT, instruction TEXT, standing INTEGER,
                created REAL, expires REAL, max_messages INTEGER, min_interval REAL, sent INTEGER, reported INTEGER,
                failed INTEGER, state TEXT, reason TEXT, last_sent REAL, seen TEXT, log TEXT, chat TEXT,
                PRIMARY KEY(owner, app, key));
        """)

    # Rows.
    def row(self, owner, app, key):
        found = self.db.execute('SELECT * FROM courier_goals WHERE owner=? AND app=? AND key=?', (owner, app, key)).fetchone()
        if found is None:
            return None
        row = dict(found)
        for name in ('seen', 'log', 'chat'):
            row[name] = json.loads(row[name] or '[]')
        return row

    def save(self, row):
        row = {**row, 'seen': json.dumps(row['seen'][-SEEN_ROWS:]), 'log': json.dumps(row['log'][-LOG_ROWS:]),
               'chat': json.dumps(row['chat'])}
        names = ', '.join(row)
        with self.db:
            self.db.execute(f'INSERT OR REPLACE INTO courier_goals ({names}) VALUES ({", ".join("?" * len(row))})',
                            tuple(row.values()))

    def active(self, owner, exclude=None):
        now = clock()
        rows = self.db.execute("SELECT key FROM courier_goals WHERE owner=? AND state='active' AND expires>? AND key!=?",
                               (owner, now, STANDING)).fetchall()
        return len([found for found in rows if found['key'] != exclude])

    def blank(self, args, key, goal, instruction, standing, ttl_min, max_messages, min_interval):
        now = clock()
        return {'owner': args['owner'], 'app': args['app'], 'key': key, 'label': args['order'], 'goal': goal,
                'instruction': instruction, 'standing': int(standing), 'created': now, 'expires': now + ttl_min * 60,
                'max_messages': max_messages, 'min_interval': min_interval, 'sent': 0, 'reported': 0, 'failed': 0,
                'state': 'active', 'reason': '', 'last_sent': 0, 'seen': [], 'log': [], 'chat': []}

    # The owner's actions that need no phone.
    def manage(self, owner, args):
        args = {**args, 'owner': owner}
        return getattr(self, args['action'])(args)

    def goal(self, args):
        owner, app, standing = args['owner'], args['app'], bool(args.get('standing'))
        key = STANDING if standing else words(args['order'])
        previous = self.row(owner, app, key)
        request_id = args.get('request_id')
        if previous and previous['log'] and previous['log'][-1].get('request_id') == request_id:
            return self.view(previous)  # a retried call
        ttl = args.get('ttl_min') or (DEFAULT_STANDING_TTL_MIN if standing else DEFAULT_TTL_MIN)
        limit = 10080 if standing else 480
        if not 1 <= ttl <= limit:
            raise Refused('invalid_arguments', 422, [{'field': 'ttl_min', 'reason': 'maximum' if ttl > 0 else 'minimum',
                                                      'expected': f'between 1 and {limit}'}])
        if not standing and self.active(owner, exclude=key) >= MAX_ACTIVE_ORDERS:
            raise Gate('too_many_active_orders')
        row = self.blank(args, key, args['goal'], args['owner_instruction'], standing, ttl,
                         args.get('max_messages') or DEFAULT_MESSAGES, args.get('min_interval_s') or MIN_INTERVAL_S)
        event = {'at': clock(), 'event': 'goal', 'request_id': request_id, 'owner_instruction': args['owner_instruction']}
        if previous:  # the counters belong to the order, not to the call
            keep = {name: previous[name] for name in ('created', 'sent', 'reported', 'failed', 'last_sent', 'seen', 'log', 'chat')}
            row.update(keep)
            event['event'] = 'resumed' if previous['state'] == 'handed_over' else 'goal'
        row['log'] = [*row['log'], event]
        self.save(row)
        return self.view(row)

    def summary(self, args):
        row = self.known(args['owner'], args['app'], args['order'])
        row['reported'] = row['sent']
        self.save(row)
        return {**self.view(row), 'unreported': 0}

    def handover(self, args):
        row = self.known(args['owner'], args['app'], args['order'])
        block = self.hand_over(row, 'agent: ' + args['reason'].strip())
        return {**block['summary'], 'handover': block}

    def known(self, owner, app, order):
        row = self.row(owner, app, words(order))
        if row is None:
            raise Gate('goal_required')
        return row

    # The device actions' gate.
    def rule(self, owner, app, order, args):
        """The order's rule (opened from a standing rule on first use); a refusal names the next call."""
        key, now = words(order), clock()
        row = self.row(owner, app, key)
        if row is None:
            standing = self.row(owner, app, STANDING)
            if standing is None or standing['state'] != 'active' or standing['expires'] <= now:
                raise Gate('goal_required', args, next_action='ask_the_owner_for_a_goal_or_propose_with_preview')
            if self.active(owner) >= MAX_ACTIVE_ORDERS:
                raise Gate('too_many_active_orders', args)
            row = self.blank({'owner': owner, 'app': app, 'order': order}, key, standing['goal'], standing['instruction'],
                             False, DEFAULT_TTL_MIN, standing['max_messages'], standing['min_interval'])
            row['log'] = [{'at': now, 'event': 'opened_from_standing_rule', 'owner_instruction': standing['instruction']}]
            self.save(row)
        return row

    def admit(self, row, args, message):
        """The gate of one reply, before any device call."""
        now = clock()
        if row['state'] == 'handed_over':
            raise Gate('handed_over', handover=self.block(row, row['reason']))
        if row['expires'] <= now:
            raise Gate('goal_expired', args, next_action='ask_the_owner_to_renew_the_goal_or_propose_with_preview')
        if row['sent'] >= row['max_messages']:
            raise Handover('message_cap_reached', self.hand_over(row, 'message_cap_reached'))
        hit = topic(message)
        if hit:
            self.note(row, {'at': now, 'request_id': args.get('request_id'), 'message': message, 'outcome': 'refused:outside_goal'})
            raise Gate('outside_goal', args, topic=hit)
        wait = row['last_sent'] + row['min_interval'] - now
        if wait > 0:
            raise Gate('rate_limited', retry_after_s=math.ceil(wait))
        if row['sent'] - row['reported'] >= UNREPORTED:
            raise Gate('report_to_owner_first', next_action='call_summary_and_tell_the_owner_then_reply')
        return row

    def note(self, row, entry):
        row['log'] = [*row['log'], entry]
        self.save(row)

    def reserve(self, row, request_id, message):
        """Counted before the tap: a crash or a timeout after it never frees the slot."""
        row['sent'] += 1
        row['last_sent'] = clock()
        self.note(row, {'at': clock(), 'request_id': request_id, 'message': message, 'outcome': 'attempt'})
        return row

    def settle(self, row, request_id, outcome):
        for entry in reversed(row['log']):
            if entry.get('request_id') == request_id and entry.get('outcome') == 'attempt':
                entry['outcome'] = outcome
                break
        if outcome == 'sent':
            row['failed'] = 0
        self.save(row)

    def failed_turn(self, row, request_id, code, message=None):
        """A turn that failed before the tap; the limit hands the chat over. Returns the handover block or None."""
        row['failed'] += 1
        entry = {'at': clock(), 'request_id': request_id, 'outcome': code}
        self.note(row, {**entry, **({'message': message} if message else {'action': 'read'})})
        if row['failed'] >= FAILED_TURNS:
            return self.hand_over(row, 'too_many_failed_turns')
        return None

    def mark_seen(self, row, rows):
        row['seen'] = [*row['seen'], *(rows_key(text) for text in rows)]
        self.save(row)

    def new_rows(self, row, rows):
        """Chat rows no earlier read showed and that are not our own sent messages."""
        ours = [words(entry['message']) for entry in row['log']
                if entry.get('message') and entry.get('outcome') in {'attempt', 'sent', 'unknown'}]
        return [text for text in rows if rows_key(text) not in row['seen'] and not any(mine in words(text) for mine in ours)]

    def hand_over(self, row, reason, chat=None, courier_row=None):
        """End the rule quietly: state, reason and an event; returns the block the owner needs."""
        if row['state'] != 'handed_over':
            row.update(state='handed_over', reason=reason, chat=list(chat or row['chat']))
            row['log'] = [*row['log'], {'at': clock(), 'event': 'handover', 'reason': reason,
                                        **({'courier_row': courier_row} if courier_row else {})}]
        elif chat:
            row['chat'] = list(chat)
        self.save(row)
        return self.block(row, row['reason'], courier_row)

    def block(self, row, reason, courier_row=None):
        return {'reason': reason, 'courier_row': courier_row, 'summary': self.view(row), 'chat': list(row['chat']),
                'next_action': 'tell_the_owner_now'}

    # What the owner is told.
    def line(self, row):
        sent = [entry['message'] for entry in row['log'] if entry.get('message') and entry.get('outcome') == 'sent']
        text = (f"Courier chat, {row['label']} ({row['app']}): {row['sent']} of {row['max_messages']} messages sent, "
                f"rule {row['state']}" + (f" ({row['reason']})" if row['state'] == 'handed_over' else ''))
        return text + (f". Last: '{sent[-1]}'" if sent else '')

    def view(self, row):
        return {'app': row['app'], 'order': row['label'], 'goal': row['goal'], 'state': row['state'], 'reason': row['reason'],
                'standing': bool(row['standing']), 'sent': row['sent'], 'max_messages': row['max_messages'],
                'min_interval_s': row['min_interval'], 'failed_turns': row['failed'], 'unreported': row['sent'] - row['reported'],
                'expires_at': row['expires'], 'log': list(row['log']), 'owner_summary': self.line(row)}

