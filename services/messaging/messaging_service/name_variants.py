"""Bounded transliteration spelling candidates, never identity links."""
import re
import unicodedata

LETTERS=dict(zip('абвгдеёжзийклмнопрстуфхцчшщъыьэюя',
    ['a','b','v','g','d','e','yo','zh','z','i','y','k','l','m','n','o','p','r','s',
     't','u','f','kh','ts','ch','sh','shch','','y','','e','yu','ya']))


def patterns(query):
    latin=''.join(LETTERS.get(c,c) for c in query.lower())
    variants={latin}
    # Common terminal spellings: Юля / Юлия / Yulia / Yuliya. This is a
    # search expansion, not proof that the four observations are one person.
    for match in list(re.finditer(r'(?:iya|ia|ya)(?=\b|$)',latin))[:2]:
        for spelling in ('iya','ia','ya'):
            variants.add(latin[:match.start()]+spelling+latin[match.end():])
    return ['%'+v.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
        for v in sorted(variants)[:8]]


def normalize(query):
    """The one query form people search matches on: NFC, whitespace collapsed, lowercase, no leading @."""
    return ' '.join(unicodedata.normalize('NFC',query).split()).lower().lstrip('@').strip()


def fold_pattern(query):
    """ANYM-663: a regular expression that reads е and ё as one letter, or None when the LIKE path is enough.

    `query` is already normalized. Only the ASCII punctuation is backslash-escaped (a backslash before a
    non-ASCII character is an error in PostgreSQL when it counts that character as alphanumeric).
    """
    if len(query)<3 or ('е' not in query and 'ё' not in query):return None
    return ''.join('[её]' if c in 'её' else c if c>'\x7f' or c.isalnum() else '\\'+c for c in query)
