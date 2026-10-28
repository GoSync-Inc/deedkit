"""The rollout class of a messaging migration (ANYM-444): `expand` only in a narrow, positively defined language.

A migration numbered HEADER_FROM or later starts with `-- rollout: expand` or `-- rollout: manual`. `classify`
returns `expand` only when every top-level statement is one of the forms below; any other statement, and any text
it cannot tokenize, is `manual`. tests/test_migration_rollout.py also applies each declared `expand` file to real
PostgreSQL and checks the catalog difference. scripts/production.py runs this file, taken from the candidate commit,
to name the class in its SQL pause: standard library only, no I/O, nothing is applied from it.
"""
import re

# 039 and 040 merged before the header rule; applied files are never edited, so they have no header (manual).
HEADER_FROM = 41
HEADERS = {'-- rollout: expand': 'expand', '-- rollout: manual': 'manual'}
# SET LOCAL bounds: a lock wait no longer than a request's 5 s statement timeout, and the 180 s that migrate() sets.
TIMEOUT_LIMITS_MS = {'lock_timeout': 5000, 'statement_timeout': 180000}
# Tokens end where PostgreSQL's scanner (scan.l) ends them: its ASCII whitespace, a line comment ends at \n or \r.
# Quotes are scanned by hand in tokens(). Whatever the scanner reads otherwise (U&, B, X and N strings, a string
# continued after a newline, a number with trailing junk, any other character) is Untokenizable, never re-tokenized.
TOKEN = re.compile(r"""(?P<space>[ \t\n\r\f]+)|(?P<line>--[^\n\r]*)|(?P<block>/\*)
    |(?P<dollar>\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$)|(?P<number>(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)
    |(?P<word>[A-Za-z_][A-Za-z0-9_$]*)|(?P<cast>::)|(?P<punct>[(),;.\[\]])|(?P<op>[-+*/<>=~!@#%^&|`?]+)""", re.X)
PREFIXED = re.compile(r"""[uU]&['"]|[bBxXnN]'""")
JUNK = re.compile(r'[A-Za-z0-9_$.]')
# What may stand between a closed string and a quote that PostgreSQL would read as its continuation.
CONTINUATION = re.compile(r'(?:[ \t\n\r\f]|--[^\n\r]*)*')
# Words that end a column type: the column's options follow.
COLUMN_OPTIONS = {'not', 'null', 'default', 'check', 'primary', 'unique', 'references', 'constraint', 'generated',
                  'collate', 'deferrable', 'initially', 'compression', 'storage'}
SERIAL = {'serial', 'serial2', 'serial4', 'serial8', 'smallserial', 'bigserial'}
NEW_TABLE_DEFAULTS = {'now', 'gen_random_uuid'}
TEMPLATE = ('begin if not ( select convalidated from pg_constraint where conrelid = $s :: regclass and conname = $s ) '
            'then raise exception $s ; end if ; end').split()


class Untokenizable(ValueError):
    pass


class Manual(Exception):
    pass


def closing(text, at, quote, escapes=False):
    """The index after the quote that ends the literal whose body starts at `at`, as PostgreSQL ends it: a doubled
    quote is part of the body and, with `escapes` (E'...'), a backslash takes the next character, a newline too."""
    end = -1
    while True:
        if end < at: end = text.find(quote, at)
        slash = text.find('\\', at, end if end >= 0 else len(text)) if escapes else -1
        if slash >= 0: at = slash + 2
        elif end < 0: raise Untokenizable(at)
        elif text.startswith(quote, end + 1): at = end + 2
        else: return end + 1


def tokens(text):
    """(kind, value) pairs; words are lower-cased, quotes and comments resolved. Anything else is Untokenizable."""
    found, at = [], 0
    while at < len(text):
        if PREFIXED.match(text, at): raise Untokenizable(at)
        if text.startswith(("'", "e'", "E'"), at):
            escapes = text[at] != "'"
            start = at + 1 + escapes
            at = closing(text, start, "'", escapes)
            if text.startswith("'", CONTINUATION.match(text, at).end()): raise Untokenizable(at)
            body = text[start:at - 1]
            found.append(('estring', body) if escapes else ('string', body.replace("''", "'")))
            continue
        if text.startswith('"', at):
            end = closing(text, at + 1, '"')
            if end == at + 2: raise Untokenizable(at)  # a zero-length identifier
            found.append(('ident', text[at + 1:end - 1].replace('""', '"')))
            at = end
            continue
        match = TOKEN.match(text, at)
        if not match:
            raise Untokenizable(at)
        kind, value, at = match.lastgroup, match.group(), match.end()
        if kind == 'block':
            depth = 1
            while depth:
                close, open_ = text.find('*/', at), text.find('/*', at)
                if close < 0: raise Untokenizable(at)
                if 0 <= open_ < close: depth, at = depth + 1, open_ + 2
                else: depth, at = depth - 1, close + 2
        elif kind == 'dollar':
            end = text.find(value, at)
            if end < 0: raise Untokenizable(at)
            found.append(('dollar', text[at:end]))
            at = end + len(value)
        elif kind == 'op' and ('--' in value or '/*' in value) or kind == 'number' and JUNK.match(text, at):
            raise Untokenizable(at)
        elif kind == 'word': found.append(('word', value.lower()))
        elif kind not in ('space', 'line'): found.append((kind, value))
    return found


def statements(text):
    """The top-level statements of `text`, each a list of tokens; empty statements are dropped."""
    result, current = [], []
    for token in tokens(text):
        if token == ('punct', ';'):
            if current: result.append(current)
            current = []
        else: current.append(token)
    return result + ([current] if current else [])


class Cursor:
    def __init__(self, items):
        self.items, self.at = items, 0

    def peek(self, offset=0):
        return self.items[self.at + offset] if self.at + offset < len(self.items) else (None, None)

    def next(self):
        if self.at >= len(self.items): raise Manual('ends early')
        self.at += 1
        return self.items[self.at - 1]

    def word(self, *values):
        kind, value = self.peek()
        if kind == 'word' and value in values:
            self.at += 1
            return value
        return None

    def expect(self, *values):
        value = self.word(*values)
        if value is None: raise Manual(f'expected {values}')
        return value

    def take(self, kind, value):
        if self.peek() == (kind, value):
            self.at += 1
            return True
        return False

    def done(self):
        return self.at == len(self.items)

    def end(self):
        if not self.done(): raise Manual('trailing tokens')

    def name(self):
        """A table, column or constraint name; `public.` is the only schema."""
        kind, value = self.next()
        if kind not in ('word', 'ident'): raise Manual('not a name')
        if (kind, value) == ('word', 'public') and self.take('punct', '.'): return self.name()
        return value

    def group(self, opening='(', closing=')'):
        """The tokens inside one balanced bracket pair that starts here."""
        if not self.take('punct', opening): raise Manual(f'expected {opening}')
        start, depth = self.at, 1
        while depth:
            kind, value = self.next()
            if kind == 'punct' and value in '([': depth += 1
            elif kind == 'punct' and value in ')]': depth -= 1
        return self.items[start:self.at - 1]


def split(items, separator=('punct', ',')):
    """`items` split at the separator outside brackets."""
    parts, current, depth = [], [], 0
    for item in items:
        if item[0] == 'punct' and item[1] in '([': depth += 1
        elif item[0] == 'punct' and item[1] in ')]': depth -= 1
        if item == separator and not depth:
            parts.append(current)
            current = []
        else: current.append(item)
    return parts + [current]


def column_type(c):
    words = []
    while c.peek()[0] in ('word', 'ident') and not (c.peek()[0] == 'word' and c.peek()[1] in COLUMN_OPTIONS):
        words.append(c.next())
    # A quoted "serial" is still the serial pseudo-type, which creates a sequence (Sol r3, ANYM-596).
    if not words or words[0][1] in SERIAL: raise Manual('no type or a serial type')
    if c.peek() == ('punct', '('):
        if any(item != ('punct', ',') and item[0] != 'number' for item in c.group()): raise Manual('type modifier')
    while c.peek() == ('punct', '['):
        if any(kind != 'number' for kind, value in c.group('[', ']')): raise Manual('array bound')


def literal(c, *, cast=True):
    if c.take('op', '-') and c.peek()[0] != 'number': raise Manual('sign')
    kind, value = c.next()
    if not (kind in ('string', 'estring', 'number') or kind == 'word' and value in ('true', 'false', 'null')):
        raise Manual('not a literal')
    if cast and c.take('cast', '::'): column_type(c)
    return (kind, value)


def references(c, state):
    # ANYM-619 (calendar's Astra r1, ANYM-586): an unqualified name resolves through search_path, so the referenced
    # table must be one of the inventory or one this file created.
    table = c.name()
    if not (table in state['created'] or existing(state, table)): raise Manual('a reference outside the schema')
    if c.peek() == ('punct', '('): c.group()
    while True:
        if c.word('match'): c.expect('full', 'simple')
        elif c.word('on'):
            c.expect('delete', 'update')
            if c.word('set'):
                c.expect('null', 'default')
                if c.peek() == ('punct', '('): c.group()
            elif c.word('no'): c.expect('action')
            else: c.expect('cascade', 'restrict')
        else: return


def new_column(c, state):
    """A column of a table this file creates: literal, now() or gen_random_uuid() defaults and any constraint."""
    c.name()
    column_type(c)
    while not c.done():
        option = c.expect('constraint', 'not', 'null', 'default', 'check', 'primary', 'unique', 'references',
                          'deferrable', 'initially')
        if option == 'constraint': c.name()
        elif option == 'not': c.expect('null', 'deferrable')
        elif option == 'default':
            if c.peek()[0] == 'word' and c.peek()[1] in NEW_TABLE_DEFAULTS and c.peek(1) == ('punct', '('):
                c.next()
                if c.group(): raise Manual('default arguments')
            else: literal(c)
        elif option == 'check':
            c.group()
            if c.word('no'): c.expect('inherit')
        elif option == 'primary': c.expect('key')
        elif option == 'references': references(c, state)
        elif option == 'initially': c.expect('deferred', 'immediate')


def create_table(c, state):
    c.expect('create')
    c.expect('table')
    if c.word('if'): c.expect('not'), c.expect('exists')
    table = c.name()
    if table in state['created'] or state['existing'] is not None and table in state['existing']:
        raise Manual('not a new table')
    elements = c.group()
    c.end()
    state['created'].add(table)  # a foreign key may reference its own table
    for element in split(elements):
        e = Cursor(element)
        if e.peek()[0] == 'word' and e.peek()[1] in ('constraint', 'primary', 'unique', 'check', 'foreign', 'exclude'):
            table_constraint(e, state)
        elif e.peek() == ('word', 'like'): raise Manual('LIKE')
        else: new_column(e, state)


def table_constraint(e, state):
    """`[CONSTRAINT c] CHECK (…) [NO INHERIT] | PRIMARY KEY (…) | UNIQUE (…) | FOREIGN KEY (…) REFERENCES …` of a new
    table, which references an inventory table or one this file created, then only deferral words; EXCLUDE, INCLUDE,
    WITH and the rest are manual."""
    if e.word('constraint'): e.name()
    kind = e.expect('check', 'primary', 'unique', 'foreign')
    if kind in ('primary', 'foreign'): e.expect('key')
    e.group()
    if kind == 'check' and e.word('no'): e.expect('inherit')
    if kind == 'foreign':
        e.expect('references')
        references(e, state)
    while not e.done():
        if e.word('not'): e.expect('deferrable')
        elif e.word('initially'): e.expect('deferred', 'immediate')
        else: e.expect('deferrable')


def create_index(c, state):
    """`CREATE [UNIQUE] INDEX [name] ON <new table> [USING btree] (<column> [ASC|DESC], …)` and nothing else."""
    c.expect('create')
    c.word('unique')
    c.expect('index')
    if c.word('if'): c.expect('not'), c.expect('exists')
    if c.peek() == ('word', 'concurrently'): raise Manual('CONCURRENTLY')
    if c.peek() != ('word', 'on'): c.name()
    c.expect('on')
    if c.peek() == ('word', 'only') or c.name() not in state['created']:
        raise Manual('an index on a table this file does not create')
    if c.word('using'): c.expect('btree')
    for key in split(c.group()):
        k = Cursor(key)
        if k.next()[0] not in ('word', 'ident'): raise Manual('not a plain column')
        k.word('asc', 'desc')
        k.end()
    c.end()


def existing(state, table):
    return table not in state['created'] and (state['existing'] is None or table in state['existing'])


def add_columns(c, state, table):
    if not (table in state['created'] or existing(state, table)): raise Manual('an unknown table')
    while True:
        c.expect('column')
        c.name()
        column_type(c)
        default = not_null = None
        while not c.done() and c.peek() != ('punct', ','):
            option = c.expect('null', 'not', 'default')
            if option == 'not': not_null = c.expect('null')
            elif option == 'default' and default is None: default = literal(c)
            elif option == 'default': raise Manual('two defaults')
        if not_null and default in (None, ('word', 'null')): raise Manual('NOT NULL without a literal default')
        if c.done(): return
        c.take('punct', ',')
        c.expect('add')


def widened(c, table, constraint):
    """`ALTER TABLE t ADD CONSTRAINT c CHECK (<col> IN (<literals>)) NOT VALID` or the `<@ ARRAY[...]::text[]` form."""
    c.expect('alter'), c.expect('table')
    if c.name() != table: raise Manual('another table')
    c.expect('add'), c.expect('constraint')
    if c.name() != constraint: raise Manual('another constraint')
    c.expect('check')
    check = Cursor(c.group())
    c.expect('not'), c.expect('valid')
    c.end()
    check.name()
    # The kinds the CI catalog check compares: numbers or strings in an IN list, strings in a text[].
    kinds = ('string', 'number')
    if check.word('in'): values = check.group()
    elif check.take('op', '<@'):
        kinds = ('string',)
        check.expect('array')
        values = check.group('[', ']')
        if not check.take('cast', '::'): raise Manual('no text[] cast')
        check.expect('text')
        if check.group('[', ']'): raise Manual('array bound')
    else: raise Manual('not a value list')
    check.end()
    for value in split(values):
        item = Cursor(value)
        if literal(item, cast=False)[0] not in kinds or not item.done(): raise Manual('not a literal')


def alter_table(c, state, following):
    """ADD COLUMN, or the widening pair; returns how many statements it took."""
    c.expect('alter'), c.expect('table')
    table = c.name()
    if c.word('add'):
        add_columns(c, state, table)
        return 1
    c.expect('drop'), c.expect('constraint')
    constraint = c.name()
    c.end()
    if not existing(state, table) or following is None: raise Manual('not a widening pair')
    widened(Cursor(following), table, constraint)
    return 2


def precondition(c):
    """036's template: refuse to replace a constraint that is not validated."""
    c.expect('do')
    kind, body = c.next()
    c.end()
    if kind != 'dollar': raise Manual('not a dollar-quoted body')
    items = tokens(body)
    if items[-1:] == [('punct', ';')]: items = items[:-1]
    if len(items) != len(TEMPLATE): raise Manual('not the template')
    for (kind, value), expected in zip(items, TEMPLATE):
        if not (kind == 'string' if expected == '$s' else kind in ('word', 'punct', 'op', 'cast') and value == expected):
            raise Manual('not the template')


def set_local(c):
    c.expect('set'), c.expect('local')
    setting = c.expect(*TIMEOUT_LIMITS_MS)
    if not (c.take('op', '=') or c.word('to')): raise Manual('no value')
    # `DEFAULT` only resets lock_timeout (036, 038); a statement_timeout reset to the role's value has no bound here.
    if not (setting == 'lock_timeout' and c.word('default')):
        kind, value = c.next()
        match = re.fullmatch(r'([0-9]+)(ms|s)', value) if kind == 'string' else None
        if not match or not 0 < int(match[1]) * (1000 if match[2] == 's' else 1) <= TIMEOUT_LIMITS_MS[setting]:
            raise Manual('timeout out of bounds')
    c.end()


def comment(c, state):
    """`COMMENT ON TABLE <t> | COLUMN <t>.<c> IS '<literal>'` on an inventory table or one this file created
    (ANYM-619, as calendar's copy: any other object, a schema, role or function too, is manual)."""
    c.expect('comment'), c.expect('on')
    kind = c.expect('table', 'column')
    table = c.name()
    if not (table in state['created'] or existing(state, table)): raise Manual("a comment outside the schema's tables")
    if kind == 'column':
        # One identifier: `name()` would read `t.public.c` as column c of table public in schema t (Astra r1).
        if not c.take('punct', '.') or c.next()[0] not in ('word', 'ident'): raise Manual('not a column')
    c.expect('is')
    if c.next()[0] not in ('string', 'estring'): raise Manual('not a literal comment')
    c.end()


def classify(text, inventory=None):
    """`expand` when every statement of `text` is in the allowed language, else `manual`.

    `inventory` names the tables that exist before the file; None when unknown (any table the file does not create
    counts as existing, and PostgreSQL itself refuses a CREATE TABLE of an existing name)."""
    try:
        parsed = statements(text)
    except Untokenizable:
        return 'manual'
    state = {'created': set(), 'existing': None if inventory is None else set(inventory)}
    at = 0
    try:
        while at < len(parsed):
            c, following = Cursor(parsed[at]), parsed[at + 1] if at + 1 < len(parsed) else None
            first, second = c.peek()[1] if c.peek()[0] == 'word' else None, c.peek(1)
            taken = 1
            if first == 'set': set_local(c)
            elif first == 'create' and second == ('word', 'table'): create_table(c, state)
            elif first == 'create' and second in (('word', 'unique'), ('word', 'index')): create_index(c, state)
            elif first == 'alter': taken = alter_table(c, state, following)
            elif first == 'do': precondition(c)
            elif first == 'comment': comment(c, state)
            else: return 'manual'
            at += taken
    except (Manual, Untokenizable):
        return 'manual'
    return 'expand'


def header(text):
    """The declared class of the first line, or None."""
    return HEADERS.get(text.split('\n', 1)[0])


def header_problem(name, text):
    """Why migration file `name` breaks the header rule, or None."""
    if int(name[:3]) >= HEADER_FROM and header(text) is None:
        return f'{name} must start with "-- rollout: expand" or "-- rollout: manual"'
    return None


def rollout_class(text, inventory=None):
    """What the deploy pause names: `no header`, a declared `manual`, or `classify` of a declared `expand`."""
    declared = header(text)
    if declared is None: return 'no header'
    return 'manual' if declared == 'manual' else classify(text, inventory)


def unattended_class(text, inventory=None):
    """What `migrate-expand` may run without a person (ANYM-596): a `rollout_class` `expand` with no DROP of any
    kind, so the CHECK widening pair, which drops its constraint first, stays `manual` here."""
    if rollout_class(text, inventory) != 'expand': return 'manual'
    parsed = statements(text)
    if any(long_name(statement) for statement in parsed): return 'manual'
    return 'manual' if any(('word', 'drop') in statement for statement in parsed) else 'expand'


# PostgreSQL truncates a longer name to NAMEDATALEN-1 bytes, so a "new" table could name an existing one.
NAME_BYTES = 63


def long_name(items):
    return any(kind in ('word', 'ident') and len(value.encode()) > NAME_BYTES for kind, value in items)


# Table privileges a role file may append; ALL, TRUNCATE, TRIGGER and MAINTAIN are manual.
ROLE_PRIVILEGES = {'select', 'insert', 'update', 'delete', 'references'}
NOT_ROLES = {'public', 'group', 'current_user', 'current_role', 'session_user', 'all'}
# Unquoted words PostgreSQL reads as a value, never as a column: `USING (true='true')` would admit every row.
VALUE_WORDS = {'true', 'false', 'null', 'default', 'user', 'current_user', 'current_role', 'session_user',
               'system_user', 'current_date', 'current_time', 'current_timestamp', 'localtime', 'localtimestamp',
               'current_catalog', 'current_schema'}
# 050's policy: a permissive policy for one role on a new table, created once. $n a name, $s a string.
POLICY = ('begin if not exists ( select from pg_policies where tablename = $s and policyname = $s ) then '
          'create policy $n on $n to $n using ( $n = $s ) with check ( $n = $s ) ; end if ; end').split()


def granted_roles(base):
    """The roles the GRANT statements of `base` grant to."""
    roles = set()
    for statement in statements(base):
        if statement[:1] == [('word', 'grant')] and ('word', 'to') in statement:
            kind, value = (statement[statement.index(('word', 'to')) + 1:] or [(None, None)])[0]
            if kind in ('word', 'ident') and value not in NOT_ROLES: roles.add(value)
    return roles


def role_name(c, roles):
    kind, value = c.next()
    if kind not in ('word', 'ident') or value in NOT_ROLES or value not in roles: raise Manual('not a role of the file')
    return value


def grant(c, roles, tables):
    """`GRANT <privilege>[(<columns>)][, …] ON [TABLE] <table>[, …] TO <role>` and nothing more; with `tables`,
    each target must be one of them (PostgreSQL takes a sequence or view after `ON` too)."""
    c.expect('grant')
    while True:
        c.expect(*ROLE_PRIVILEGES)
        if c.peek() == ('punct', '('):
            for column in split(c.group()):
                if len(column) != 1 or column[0][0] not in ('word', 'ident'): raise Manual('not a column list')
        if not c.take('punct', ','): break
    c.expect('on')
    c.word('table')
    while True:
        if c.peek()[0] == 'word' and c.peek()[1] in ('all', 'schema', 'database', 'function', 'sequence'):
            raise Manual('not a table')
        table = c.name()
        if tables is not None and table not in tables: raise Manual('not a known table')
        if not c.take('punct', ','): break
    c.expect('to')
    role_name(c, roles)
    c.end()


def policy(c, roles, new_tables):
    """The 050 template: `DO $$ … CREATE POLICY … $$` for a table this release creates, on one of its columns."""
    c.expect('do')
    kind, body = c.next()
    c.end()
    if kind != 'dollar': raise Manual('not a dollar-quoted body')
    items = tokens(body)
    if len(items) != len(POLICY): raise Manual('not the template')
    slots = {'$s': [], '$n': []}
    for (kind, value), expected in zip(items, POLICY):
        if expected in slots:
            if kind not in (('string',) if expected == '$s' else ('word', 'ident')): raise Manual('not the template')
            if kind == 'word' and value in VALUE_WORDS: raise Manual('a value word in a name slot')
            slots[expected].append(value)
        elif not (kind in ('word', 'punct', 'op') and value == expected): raise Manual('not the template')
    (table_string, policy_string, using, check), (name, table, role, using_column, check_column) = (
        slots['$s'], slots['$n'])
    if (table_string, policy_string, check_column, check) != (table, name, using_column, using):
        raise Manual('the template slots disagree')
    if long_name(items): raise Manual('a name PostgreSQL would truncate')
    if table not in new_tables or using_column not in new_tables[table]:
        raise Manual('a policy on a table this release does not create, or not on its column')
    role_name(Cursor([('word', role)]), roles)


def role_class(base, candidate, new_tables, *, tables=None):
    """`additive` when `candidate` is `base` byte for byte plus appended statements that are comments, GRANTs of
    table privileges to a role `base` already grants to, or the 050 policy template on a column of one of
    `new_tables` ({table: columns}, as `new_tables()` returns); else `manual` (ANYM-596). With `tables` (the tables
    that exist), a GRANT target must be one of them or a new table. Standard library only, no I/O, like `classify`."""
    if not candidate.startswith(base): return 'manual'
    appended = candidate[len(base):]
    try:
        # The file must scan as its two parts do: the base ends between statements, outside any comment or quote.
        before, added = tokens(base), tokens(appended)
        if tokens(candidate) != before + added or before and before[-1] != ('punct', ';'): return 'manual'
        if long_name(added): return 'manual'
        roles, known = granted_roles(base), None if tables is None else set(tables) | set(new_tables)
        for statement in statements(appended):
            c = Cursor(statement)
            if c.peek() == ('word', 'grant'): grant(c, roles, known)
            elif c.peek() == ('word', 'do'): policy(c, roles, new_tables)
            else: return 'manual'
    except (Manual, Untokenizable):
        return 'manual'
    return 'additive'


def new_tables(text, created=None):
    """{table: its column names} for each CREATE TABLE of a migration, with the columns later ADD COLUMNs give
    those tables, added to a copy of `created` (the earlier files' result); `created` alone when it does not parse."""
    found = {table: set(columns) for table, columns in (created or {}).items()}
    try:
        for statement in statements(text):
            c = Cursor(statement)
            if c.word('create') and c.word('table'):
                if c.word('if'): c.expect('not'), c.expect('exists')
                table, columns = c.name(), set()
                for element in split(c.group()):
                    kind, value = element[0] if element else (None, None)
                    if kind == 'ident' or kind == 'word' and value not in (
                            'constraint', 'primary', 'unique', 'check', 'foreign', 'exclude', 'like'):
                        columns.add(value)
                found[table] = columns
            elif c.word('alter') and c.word('table') and (table := c.name()) in found:
                for part in split(statement[c.at:]):
                    p = Cursor(part)
                    if p.word('add') and p.word('column'): found[table].add(p.name())
    except (Manual, Untokenizable):
        return {table: set(columns) for table, columns in (created or {}).items()}
    return found
