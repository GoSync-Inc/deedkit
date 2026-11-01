"""The identifier regexes copied across Python, JS and Go stay one pattern; today's variants are pinned.

Chat, account, send and WhatsApp identifiers are validated by hand-copied regex text in the messaging
service (Python), the browser fallback (JS) and the native adapter (Go). Each copy is found in the source text
by its family signature, so literal style (raw string, regex literal, backticks, SQL) does not matter. A regex
body not listed here fails the test, and so does a listed variant that no longer exists: a refactor that
merges copies keeps the canonical body; a change that fixes a variant removes its line. Fixing a variant
changes what is accepted, so it is a defect card, not a refactor.
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
SOURCES = {
    'py': sorted((ROOT / 'services/messaging/messaging_service').rglob('*.py')),
    'js': [p for p in sorted((ROOT / 'whatsapp').rglob('*.mjs'))
           if 'node_modules' not in p.parts and 'tests' not in p.parts and not p.name.endswith('.test.mjs')],
    'go': [p for p in sorted((ROOT / 'whatsapp/whatsmeow').rglob('*.go')) if not p.name.endswith('_test.go')],
}
ALL = ('go', 'js', 'py')
# family -> signature: finds every copy of the family, canonical or not. The character class may be spelled
# either way round (a-zA-Z / A-Za-z) and the dot escaped or not; any bounds count, so a changed bound is a variant.
_CLASS = r'\[(?:a-zA-Z|A-Za-z)0-9_\\?\.'
SIGNATURES = {
    'chat': _CLASS + r':-\]\{\d+,\d+\}@\([^()]*\)',
    'account': _CLASS + r'-\]\{\d+,\d+\}',
    'send_id': r'send_\[[^\]]+\]\{\d+\}',
    'wid': r'\[0-9\]\{\d+,\d+\}@c\\?\.us',
    'user': r'\[0-9\]\{1,\d+\}@\(c\\?\.us\|lid\)',
}
CANONICAL = {  # body -> languages that must carry it
    'chat': {r'[a-zA-Z0-9_.:-]{1,140}@(c\.us|g\.us|lid)': ALL},
    'account': {r'[a-zA-Z0-9_.-]{1,64}': ALL},
    'send_id': {r'send_[a-f0-9]{32}': ALL},
    'wid': {r'[0-9]{5,20}@c\.us': ('go', 'py')},
    'user': {r'[0-9]{1,30}@(c\.us|lid)': ALL},
}
# Variants seen today: body -> languages. Each is a defect-card candidate unless the reason says by design.
EXPECTED_VARIANTS = {
    'chat': {
        # Unescaped dots: `.` matches any character, so `x@cXus` passes (collector_state.py read capture).
        r'[a-zA-Z0-9_.:-]{1,140}@(c.us|g.us|lid)': ('py',),
        # Same, and without g.us (whatsapp/sender.mjs sender ids).
        r'[a-zA-Z0-9_.:-]{1,140}@(c.us|lid)': ('js',),
        # By design: a sender is a person, never a group (reply_evidence.py, subscriptions.py SQL).
        r'[a-zA-Z0-9_.:-]{1,140}@(c\.us|lid)': ('py',),
    },
    'account': {
        # Not account ids: a backup-config value (backup_runner.py) and a URN tail (reviews.py); bound 128.
        r'[a-zA-Z0-9_.-]{1,128}': ('py',),
        # Not an account id: a Gemini model id (enrichment_providers.py); bound 120.
        r'[A-Za-z0-9_.-]{1,120}': ('py',),
    },
    'send_id': {},
    'wid': {
        # Unescaped dot (whatsapp_collector.py expected_wid).
        r'[0-9]{5,20}@c.us': ('py',),
        # Lower bound 1, not 5: the phone-number half of an identity pair (identity.mjs, outgoing.py).
        r'[0-9]{1,20}@c\.us': ('js', 'py'),
    },
    'user': {},
}


def _inventory():
    found = {family: {} for family in SIGNATURES}
    for lang, paths in SOURCES.items():
        for path in paths:
            text = path.read_text()
            if lang != 'py':
                text = text.replace('\\d', '[0-9]')  # JS and Go \d are ASCII digits: the same body
            # A Python \d (Unicode digits) is not rewritten, so it never matches a [0-9] signature; no Python copy uses one.
            for family, signature in SIGNATURES.items():
                for body in re.findall(signature, text):
                    found[family].setdefault(body, {}).setdefault(lang, set()).add(path.name)
    return found


def _languages(found, family):
    return {body: tuple(sorted(langs)) for body, langs in found[family].items()}


# spec: ANYM-162
def test_every_identifier_copy_is_canonical_or_a_listed_variant():
    found = _inventory()
    for family in SIGNATURES:
        expected = {body: tuple(sorted(langs)) for body, langs in EXPECTED_VARIANTS[family].items()}
        canonical = {body: tuple(sorted(langs)) for body, langs in CANONICAL[family].items()}
        observed = _languages(found, family)
        # Canonical bodies carry at least their languages (a Python-only extra copy of a Go body is fine).
        for body, langs in canonical.items():
            assert set(langs) <= set(observed.get(body, ())), (family, body, observed.get(body))
        variants = {body: langs for body, langs in observed.items() if body not in canonical}
        assert variants == expected, (family, {b: sorted(found[family][b].items()) for b in variants})


# spec: ANYM-162
def test_canonical_chat_account_and_send_patterns_accept_and_refuse_the_same_samples():
    # Python is the one engine run here; the bodies above are textually identical in JS and Go, and use only
    # classes, bounds, groups and alternation, which the three engines read alike.
    chat = re.compile(next(iter(CANONICAL['chat'])))
    account = re.compile(next(iter(CANONICAL['account'])))
    send = re.compile(next(iter(CANONICAL['send_id'])))
    wid = re.compile(next(iter(CANONICAL['wid'])))
    user = re.compile(next(iter(CANONICAL['user'])))
    assert all(chat.fullmatch(v) for v in ('120363000000000000@g.us', '491700000000-1234567890@g.us', '1@lid', '1@c.us'))
    assert not any(chat.fullmatch(v) for v in ('x@cXus', 'x@g_us', '@lid', 'a' * 141 + '@lid', 'a b@lid', '1@s.whatsapp.net'))
    assert account.fullmatch('a' * 64) and not account.fullmatch('a' * 65) and not account.fullmatch('a b')
    assert send.fullmatch('send_' + 'a' * 32) and not send.fullmatch('send_' + 'A' * 32)
    assert not send.fullmatch('send_' + 'a' * 31) and not send.fullmatch('send_' + 'a' * 33)
    assert wid.fullmatch('49170000000@c.us') and not wid.fullmatch('1234@c.us') and not wid.fullmatch('49170000000@lid')
    assert user.fullmatch('1@lid') and user.fullmatch('1@c.us') and not user.fullmatch('1@g.us')
    assert not user.fullmatch('1' * 31 + '@lid') and not user.fullmatch('１@lid')


# spec: ANYM-162
def test_unescaped_dot_variants_accept_what_the_canonical_bodies_refuse():
    # Documents the drift behind the listed variants; the sample is synthetic.
    canonical = re.compile(next(iter(CANONICAL['chat'])))
    for body in EXPECTED_VARIANTS['chat']:
        loose = re.compile(body)
        if '(c.us' in body:
            assert loose.fullmatch('x@cXus') and not canonical.fullmatch('x@cXus')
    assert re.fullmatch(r'[0-9]{5,20}@c.us', '49170000000@cXus')
    assert not re.fullmatch(next(iter(CANONICAL['wid'])), '49170000000@cXus')
