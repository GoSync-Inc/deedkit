"""The rollout class of a calendar migration and roles.sql text (ANYM-586): a copy of messaging's
migration_rollout.py (ANYM-444, ANYM-596) for the calendar schema.

Differences from the messaging copy: names are unqualified or qualified by the calendar schema (`fcs.`, the store's
schema in tests), never `public.`; a migration file is `schema_vN.sql` and from version HEADER_FROM starts with
`-- rollout: expand` or `-- rollout: manual`; appended roles.sql text may grant to several of the file's roles in one
GRANT and may use the guarded template `DO $$ BEGIN IF to_regclass('fcs.<t>') IS NOT NULL THEN <GRANT …>; … END IF;
END $$` for a table <t> the range creates; there is no policy template. `plan` is the positive classification of a
base..candidate range before anything runs. Standard library only, no I/O, nothing is applied from it.
"""

import hashlib
import re

# Versions 1-5 merged before the header rule; applied files are never edited, so they have no header (manual).
HEADER_FROM = 6
HEADERS = {"-- rollout: expand": "expand", "-- rollout: manual": "manual"}
# SET LOCAL bounds: a lock wait no longer than a request's 5 s statement timeout, and the 180 s that migrate() sets.
TIMEOUT_LIMITS_MS = {"lock_timeout": 5000, "statement_timeout": 180000}
# Tokens end where PostgreSQL's scanner (scan.l) ends them: its ASCII whitespace, a line comment ends at \n or \r.
# Quotes are scanned by hand in tokens(). Whatever the scanner reads otherwise (U&, B, X and N strings, a string
# continued after a newline, a number with trailing junk, any other character) is Untokenizable, never re-tokenized.
TOKEN = re.compile(
    r"""(?P<space>[ \t\n\r\f]+)|(?P<line>--[^\n\r]*)|(?P<block>/\*)
    |(?P<dollar>\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$)|(?P<number>(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)
    |(?P<word>[A-Za-z_][A-Za-z0-9_$]*)|(?P<cast>::)|(?P<punct>[(),;.\[\]])|(?P<op>[-+*/<>=~!@#%^&|`?]+)""",
    re.X,
)
PREFIXED = re.compile(r"""[uU]&['"]|[bBxXnN]'""")
JUNK = re.compile(r"[A-Za-z0-9_$.]")
# What may stand between a closed string and a quote that PostgreSQL would read as its continuation.
CONTINUATION = re.compile(r"(?:[ \t\n\r\f]|--[^\n\r]*)*")
# Words that end a column type: the column's options follow.
COLUMN_OPTIONS = {
    "not",
    "null",
    "default",
    "check",
    "primary",
    "unique",
    "references",
    "constraint",
    "generated",
    "collate",
    "deferrable",
    "initially",
    "compression",
    "storage",
}
SERIAL = {"serial", "serial2", "serial4", "serial8", "smallserial", "bigserial"}
NEW_TABLE_DEFAULTS = {"now", "gen_random_uuid"}
TEMPLATE = (
    "begin if not ( select convalidated from pg_constraint where conrelid = $s :: regclass and conname = $s ) "
    "then raise exception $s ; end if ; end"
).split()


class Untokenizable(ValueError):
    pass


class Manual(Exception):
    pass


def closing(text, at, quote, escapes=False):
    """The index after the quote that ends the literal whose body starts at `at`, as PostgreSQL ends it: a doubled
    quote is part of the body and, with `escapes` (E'...'), a backslash takes the next character, a newline too."""
    end = -1
    while True:
        if end < at:
            end = text.find(quote, at)
        slash = text.find("\\", at, end if end >= 0 else len(text)) if escapes else -1
        if slash >= 0:
            at = slash + 2
        elif end < 0:
            raise Untokenizable(at)
        elif text.startswith(quote, end + 1):
            at = end + 2
        else:
            return end + 1


def tokens(text):
    """(kind, value) pairs; words are lower-cased, quotes and comments resolved. Anything else is Untokenizable."""
    found, at = [], 0
    while at < len(text):
        if PREFIXED.match(text, at):
            raise Untokenizable(at)
        if text.startswith(("'", "e'", "E'"), at):
            escapes = text[at] != "'"
            start = at + 1 + escapes
            at = closing(text, start, "'", escapes)
            if text.startswith("'", CONTINUATION.match(text, at).end()):
                raise Untokenizable(at)
            body = text[start : at - 1]
            found.append(("estring", body) if escapes else ("string", body.replace("''", "'")))
            continue
        if text.startswith('"', at):
            end = closing(text, at + 1, '"')
            if end == at + 2:
                raise Untokenizable(at)  # a zero-length identifier
            found.append(("ident", text[at + 1 : end - 1].replace('""', '"')))
            at = end
            continue
        match = TOKEN.match(text, at)
        if not match:
            raise Untokenizable(at)
        kind, value, at = match.lastgroup, match.group(), match.end()
        if kind == "block":
            depth = 1
            while depth:
                close, open_ = text.find("*/", at), text.find("/*", at)
                if close < 0:
                    raise Untokenizable(at)
                if 0 <= open_ < close:
                    depth, at = depth + 1, open_ + 2
                else:
                    depth, at = depth - 1, close + 2
        elif kind == "dollar":
            end = text.find(value, at)
            if end < 0:
                raise Untokenizable(at)
            found.append(("dollar", text[at:end]))
            at = end + len(value)
        elif kind == "op" and ("--" in value or "/*" in value) or kind == "number" and JUNK.match(text, at):
            raise Untokenizable(at)
        elif kind == "word":
            found.append(("word", value.lower()))
        elif kind not in ("space", "line"):
            found.append((kind, value))
    return found


def scoped(items, schema):
    """`items` with each leading `<schema>.` qualifier before a name removed: the calendar schema is the only one, and the
    migration's search_path starts with it. Any other qualifier stays and fails the grammar."""
    out, at = [], 0
    while at < len(items):
        # Only a leading qualifier: in `t.fcs.c` the schema word is a middle part of a longer path (Astra r2).
        if (
            not (at and items[at - 1] == ("punct", "."))
            and items[at][0] in ("word", "ident")
            and items[at][1] == schema
            and items[at + 1 : at + 2] == [("punct", ".")]
            and items[at + 2 : at + 3]
            and items[at + 2][0] in ("word", "ident")
        ):
            at += 2
            continue
        out.append(items[at])
        at += 1
    return out


def statements(text, schema="fcs"):
    """The top-level statements of `text`, each a list of tokens; empty statements are dropped."""
    result, current = [], []
    for token in scoped(tokens(text), schema):
        if token == ("punct", ";"):
            if current:
                result.append(current)
            current = []
        else:
            current.append(token)
    return result + ([current] if current else [])


class Cursor:
    def __init__(self, items):
        self.items, self.at = items, 0

    def peek(self, offset=0):
        return self.items[self.at + offset] if self.at + offset < len(self.items) else (None, None)

    def next(self):
        if self.at >= len(self.items):
            raise Manual("ends early")
        self.at += 1
        return self.items[self.at - 1]

    def word(self, *values):
        kind, value = self.peek()
        if kind == "word" and value in values:
            self.at += 1
            return value
        return None

    def expect(self, *values):
        value = self.word(*values)
        if value is None:
            raise Manual(f"expected {values}")
        return value

    def take(self, kind, value):
        if self.peek() == (kind, value):
            self.at += 1
            return True
        return False

    def done(self):
        return self.at == len(self.items)

    def end(self):
        if not self.done():
            raise Manual("trailing tokens")

    def name(self):
        """A table, column or constraint name; `scoped` already removed the calendar schema's qualifier."""
        kind, value = self.next()
        if kind not in ("word", "ident"):
            raise Manual("not a name")
        return value

    def group(self, opening="(", closing=")"):
        """The tokens inside one balanced bracket pair that starts here."""
        if not self.take("punct", opening):
            raise Manual(f"expected {opening}")
        start, depth = self.at, 1
        while depth:
            kind, value = self.next()
            if kind == "punct" and value in "([":
                depth += 1
            elif kind == "punct" and value in ")]":
                depth -= 1
        return self.items[start : self.at - 1]


def split(items, separator=("punct", ",")):
    """`items` split at the separator outside brackets."""
    parts, current, depth = [], [], 0
    for item in items:
        if item[0] == "punct" and item[1] in "([":
            depth += 1
        elif item[0] == "punct" and item[1] in ")]":
            depth -= 1
        if item == separator and not depth:
            parts.append(current)
            current = []
        else:
            current.append(item)
    return parts + [current]


def column_type(c):
    words = []
    while c.peek()[0] in ("word", "ident") and not (c.peek()[0] == "word" and c.peek()[1] in COLUMN_OPTIONS):
        words.append(c.next())
    # A quoted "serial" is still the serial pseudo-type, which creates a sequence (Sol r3, ANYM-596).
    if not words or words[0][1] in SERIAL:
        raise Manual("no type or a serial type")
    if c.peek() == ("punct", "("):
        if any(item != ("punct", ",") and item[0] != "number" for item in c.group()):
            raise Manual("type modifier")
    while c.peek() == ("punct", "["):
        if any(kind != "number" for kind, value in c.group("[", "]")):
            raise Manual("array bound")


def literal(c, *, cast=True):
    if c.take("op", "-") and c.peek()[0] != "number":
        raise Manual("sign")
    kind, value = c.next()
    if not (kind in ("string", "estring", "number") or kind == "word" and value in ("true", "false", "null")):
        raise Manual("not a literal")
    if cast and c.take("cast", "::"):
        column_type(c)
    return (kind, value)


def references(c, state):
    # Calendar (Astra r1, ANYM-586): an unqualified name could resolve to `public` through search_path, so the
    # referenced table must be one of the schema's tables or one this file created.
    table = c.name()
    if not (table in state["created"] or existing(state, table)):
        raise Manual("a reference outside the schema")
    if c.peek() == ("punct", "("):
        c.group()
    while True:
        if c.word("match"):
            c.expect("full", "simple")
        elif c.word("on"):
            c.expect("delete", "update")
            if c.word("set"):
                c.expect("null", "default")
                if c.peek() == ("punct", "("):
                    c.group()
            elif c.word("no"):
                c.expect("action")
            else:
                c.expect("cascade", "restrict")
        else:
            return


def new_column(c, state):
    """A column of a table this file creates: literal, now() or gen_random_uuid() defaults and any constraint."""
    c.name()
    column_type(c)
    while not c.done():
        option = c.expect(
            "constraint",
            "not",
            "null",
            "default",
            "check",
            "primary",
            "unique",
            "references",
            "deferrable",
            "initially",
        )
        if option == "constraint":
            c.name()
        elif option == "not":
            c.expect("null", "deferrable")
        elif option == "default":
            if c.peek()[0] == "word" and c.peek()[1] in NEW_TABLE_DEFAULTS and c.peek(1) == ("punct", "("):
                c.next()
                if c.group():
                    raise Manual("default arguments")
            else:
                literal(c)
        elif option == "check":
            c.group()
            if c.word("no"):
                c.expect("inherit")
        elif option == "primary":
            c.expect("key")
        elif option == "references":
            references(c, state)
        elif option == "initially":
            c.expect("deferred", "immediate")


def create_table(c, state):
    c.expect("create")
    c.expect("table")
    if c.word("if"):
        c.expect("not"), c.expect("exists")
    table = c.name()
    if table in state["created"] or state["existing"] is not None and table in state["existing"]:
        raise Manual("not a new table")
    elements = c.group()
    c.end()
    # Known before its elements, so a self-referencing foreign key passes `references` (Astra r2).
    state["created"].add(table)
    for element in split(elements):
        e = Cursor(element)
        if e.peek()[0] == "word" and e.peek()[1] in (
            "constraint",
            "primary",
            "unique",
            "check",
            "foreign",
            "exclude",
        ):
            table_constraint(e, state)
        elif e.peek() == ("word", "like"):
            raise Manual("LIKE")
        else:
            new_column(e, state)


def table_constraint(e, state):
    """`[CONSTRAINT c] CHECK (…) [NO INHERIT] | PRIMARY KEY (…) | UNIQUE (…) | FOREIGN KEY (…) REFERENCES …` of a new
    table, which may reference a table of the schema, then only deferral words; EXCLUDE, INCLUDE, WITH and the rest
    are manual."""
    if e.word("constraint"):
        e.name()
    kind = e.expect("check", "primary", "unique", "foreign")
    if kind in ("primary", "foreign"):
        e.expect("key")
    e.group()
    if kind == "check" and e.word("no"):
        e.expect("inherit")
    if kind == "foreign":
        e.expect("references")
        references(e, state)
    while not e.done():
        if e.word("not"):
            e.expect("deferrable")
        elif e.word("initially"):
            e.expect("deferred", "immediate")
        else:
            e.expect("deferrable")


def create_index(c, state):
    """`CREATE [UNIQUE] INDEX [name] ON <new table> [USING btree] (<column> [ASC|DESC], …)` and nothing else."""
    c.expect("create")
    c.word("unique")
    c.expect("index")
    if c.word("if"):
        c.expect("not"), c.expect("exists")
    if c.peek() == ("word", "concurrently"):
        raise Manual("CONCURRENTLY")
    if c.peek() != ("word", "on"):
        c.name()
    c.expect("on")
    if c.peek() == ("word", "only") or c.name() not in state["created"]:
        raise Manual("an index on a table this file does not create")
    if c.word("using"):
        c.expect("btree")
    for key in split(c.group()):
        k = Cursor(key)
        if k.next()[0] not in ("word", "ident"):
            raise Manual("not a plain column")
        k.word("asc", "desc")
        k.end()
    c.end()


def existing(state, table):
    return table not in state["created"] and (state["existing"] is None or table in state["existing"])


def add_columns(c, state, table):
    if not (table in state["created"] or existing(state, table)):
        raise Manual("an unknown table")
    while True:
        c.expect("column")
        c.name()
        column_type(c)
        default = not_null = None
        while not c.done() and c.peek() != ("punct", ","):
            option = c.expect("null", "not", "default")
            if option == "not":
                not_null = c.expect("null")
            elif option == "default" and default is None:
                default = literal(c)
            elif option == "default":
                raise Manual("two defaults")
        if not_null and default in (None, ("word", "null")):
            raise Manual("NOT NULL without a literal default")
        if c.done():
            return
        c.take("punct", ",")
        c.expect("add")


def widened(c, table, constraint):
    """`ALTER TABLE t ADD CONSTRAINT c CHECK (<col> IN (<literals>)) NOT VALID` or the `<@ ARRAY[...]::text[]` form."""
    c.expect("alter"), c.expect("table")
    if c.name() != table:
        raise Manual("another table")
    c.expect("add"), c.expect("constraint")
    if c.name() != constraint:
        raise Manual("another constraint")
    c.expect("check")
    check = Cursor(c.group())
    c.expect("not"), c.expect("valid")
    c.end()
    check.name()
    # The kinds the CI catalog check compares: numbers or strings in an IN list, strings in a text[].
    kinds = ("string", "number")
    if check.word("in"):
        values = check.group()
    elif check.take("op", "<@"):
        kinds = ("string",)
        check.expect("array")
        values = check.group("[", "]")
        if not check.take("cast", "::"):
            raise Manual("no text[] cast")
        check.expect("text")
        if check.group("[", "]"):
            raise Manual("array bound")
    else:
        raise Manual("not a value list")
    check.end()
    for value in split(values):
        item = Cursor(value)
        if literal(item, cast=False)[0] not in kinds or not item.done():
            raise Manual("not a literal")


def alter_table(c, state, following):
    """ADD COLUMN, or the widening pair; returns how many statements it took."""
    c.expect("alter"), c.expect("table")
    table = c.name()
    if c.word("add"):
        add_columns(c, state, table)
        return 1
    c.expect("drop"), c.expect("constraint")
    constraint = c.name()
    c.end()
    if not existing(state, table) or following is None:
        raise Manual("not a widening pair")
    widened(Cursor(following), table, constraint)
    return 2


def precondition(c):
    """036's template: refuse to replace a constraint that is not validated."""
    c.expect("do")
    kind, body = c.next()
    c.end()
    if kind != "dollar":
        raise Manual("not a dollar-quoted body")
    items = tokens(body)
    if items[-1:] == [("punct", ";")]:
        items = items[:-1]
    if len(items) != len(TEMPLATE):
        raise Manual("not the template")
    for (kind, value), expected in zip(items, TEMPLATE):
        if not (
            kind == "string"
            if expected == "$s"
            else kind in ("word", "punct", "op", "cast") and value == expected
        ):
            raise Manual("not the template")


def set_local(c):
    c.expect("set"), c.expect("local")
    setting = c.expect(*TIMEOUT_LIMITS_MS)
    if not (c.take("op", "=") or c.word("to")):
        raise Manual("no value")
    # `DEFAULT` only resets lock_timeout (036, 038); a statement_timeout reset to the role's value has no bound here.
    if not (setting == "lock_timeout" and c.word("default")):
        kind, value = c.next()
        match = re.fullmatch(r"([0-9]+)(ms|s)", value) if kind == "string" else None
        if (
            not match
            or not 0 < int(match[1]) * (1000 if match[2] == "s" else 1) <= TIMEOUT_LIMITS_MS[setting]
        ):
            raise Manual("timeout out of bounds")
    c.end()


def comment(c, state):
    """`COMMENT ON TABLE <t> | COLUMN <t>.<c> IS '<literal>'` on a table of the schema or one this file created
    (calendar, Astra r1: messaging's copy takes any object, a schema or role too)."""
    c.expect("comment"), c.expect("on")
    kind = c.expect("table", "column")
    table = c.name()
    if not (table in state["created"] or existing(state, table)):
        raise Manual("a comment outside the schema's tables")
    if kind == "column":
        if not c.take("punct", "."):
            raise Manual("not a column")
        c.name()
    c.expect("is")
    if c.next()[0] not in ("string", "estring"):
        raise Manual("not a literal comment")
    c.end()


def classify(text, inventory=None, schema="fcs"):
    """`expand` when every statement of `text` is in the allowed language, else `manual`.

    `inventory` names the tables of `schema` that exist before the file; None when unknown (any table the file does
    not create counts as existing, and PostgreSQL itself refuses a CREATE TABLE of an existing name)."""
    try:
        parsed = statements(text, schema)
    except Untokenizable:
        return "manual"
    state = {"created": set(), "existing": None if inventory is None else set(inventory)}
    at = 0
    try:
        while at < len(parsed):
            c, following = Cursor(parsed[at]), parsed[at + 1] if at + 1 < len(parsed) else None
            first, second = c.peek()[1] if c.peek()[0] == "word" else None, c.peek(1)
            taken = 1
            if first == "set":
                set_local(c)
            elif first == "create" and second == ("word", "table"):
                create_table(c, state)
            elif first == "create" and second in (("word", "unique"), ("word", "index")):
                create_index(c, state)
            elif first == "alter":
                taken = alter_table(c, state, following)
            elif first == "do":
                precondition(c)
            elif first == "comment":
                comment(c, state)
            else:
                return "manual"
            at += taken
    except (Manual, Untokenizable):
        return "manual"
    return "expand"


def header(text):
    """The declared class of the first line, or None."""
    return HEADERS.get(text.split("\n", 1)[0])


def header_problem(version, text):
    """Why migration `version` breaks the header rule, or None."""
    if version >= HEADER_FROM and header(text) is None:
        return f'schema_v{version}.sql must start with "-- rollout: expand" or "-- rollout: manual"'
    return None


def rollout_class(text, inventory=None, schema="fcs"):
    """What a pause names: `no header`, a declared `manual`, or `classify` of a declared `expand`."""
    declared = header(text)
    if declared is None:
        return "no header"
    return "manual" if declared == "manual" else classify(text, inventory, schema)


def unattended_class(text, inventory=None, schema="fcs"):
    """What `migrate-expand` may run without a person (ANYM-596): a `rollout_class` `expand` with no DROP of any
    kind, so the CHECK widening pair, which drops its constraint first, stays `manual` here."""
    if rollout_class(text, inventory, schema) != "expand":
        return "manual"
    parsed = statements(text, schema)
    if any(long_name(statement) for statement in parsed):
        return "manual"
    return "manual" if any(("word", "drop") in statement for statement in parsed) else "expand"


# PostgreSQL truncates a longer name to NAMEDATALEN-1 bytes, so a "new" table could name an existing one.
NAME_BYTES = 63


def long_name(items):
    return any(kind in ("word", "ident") and len(value.encode()) > NAME_BYTES for kind, value in items)


# Table privileges roles.sql may append; ALL, TRUNCATE, TRIGGER and MAINTAIN are manual.
ROLE_PRIVILEGES = {"select", "insert", "update", "delete", "references"}
NOT_ROLES = {"public", "group", "current_user", "current_role", "session_user", "all"}
# roles.sql's guard for a table a migration adds (migrations 2, 4 and 5): $s the table's schema-qualified name.
GUARD_OPEN = "begin if to_regclass ( $s ) is not null then".split()
GUARD_CLOSE = "end if ; end".split()


def granted_roles(base, schema="fcs"):
    """The roles the top-level GRANT statements of `base` grant to."""
    roles = set()
    for statement in statements(base, schema):
        if statement[:1] == [("word", "grant")] and ("word", "to") in statement:
            for part in split(statement[statement.index(("word", "to")) + 1 :]):
                kind, value = (part or [(None, None)])[0]
                if len(part) == 1 and kind in ("word", "ident") and value not in NOT_ROLES:
                    roles.add(value)
    return roles


def role_name(c, roles):
    kind, value = c.next()
    if kind not in ("word", "ident") or value in NOT_ROLES or value not in roles:
        raise Manual("not a role of the file")
    return value


def grant(c, roles, tables):
    """`GRANT <privilege>[(<columns>)][, …] ON [TABLE] <table>[, …] TO <role>[, …]` and nothing more; with `tables`,
    each target must be one of them (PostgreSQL takes a sequence or view after `ON` too)."""
    c.expect("grant")
    while True:
        c.expect(*ROLE_PRIVILEGES)
        if c.peek() == ("punct", "("):
            for column in split(c.group()):
                if len(column) != 1 or column[0][0] not in ("word", "ident"):
                    raise Manual("not a column list")
        if not c.take("punct", ","):
            break
    c.expect("on")
    c.word("table")
    while True:
        if c.peek()[0] == "word" and c.peek()[1] in ("all", "schema", "database", "function", "sequence"):
            raise Manual("not a table")
        table = c.name()
        if tables is not None and table not in tables:
            raise Manual("not a known table")
        if not c.take("punct", ","):
            break
    c.expect("to")
    while True:
        role_name(c, roles)
        if not c.take("punct", ","):
            break
    c.end()


def guarded(c, roles, tables, new_tables, schema):
    """The roles.sql guard: `DO $$ BEGIN IF to_regclass('<schema>.<t>') IS NOT NULL THEN <GRANT>; … END IF; END $$`
    with <t> a table this release creates and only GRANTs of `grant`'s form inside."""
    c.expect("do")
    kind, body = c.next()
    c.end()
    if kind != "dollar":
        raise Manual("not a dollar-quoted body")
    items = scoped(tokens(body), schema)
    if items[-1:] == [("punct", ";")]:
        items = items[:-1]
    head, tail = items[: len(GUARD_OPEN)], items[len(items) - len(GUARD_CLOSE) :]
    if len(items) <= len(GUARD_OPEN) + len(GUARD_CLOSE) or tail != [
        ("punct", value) if value == ";" else ("word", value) for value in GUARD_CLOSE
    ]:
        raise Manual("not the template")
    for (kind, value), expected in zip(head, GUARD_OPEN):
        if not (kind == "string" if expected == "$s" else kind in ("word", "punct") and value == expected):
            raise Manual("not the template")
    if long_name(items):
        raise Manual("a name PostgreSQL would truncate")
    # to_regclass folds an unquoted name to lower case, so the guard names only a plain lower-case new table: a
    # quoted or mixed-case one would read NULL and skip its grants silently (Astra r1).
    guards = {schema + "." + table for table in new_tables if re.fullmatch(r"[a-z_][a-z0-9_]*", table)}
    if head[4][1] not in guards:
        raise Manual("a guard on a table this release does not create")
    inner, current = [], []
    for token in items[len(GUARD_OPEN) : len(items) - len(GUARD_CLOSE)]:
        if token == ("punct", ";"):
            if not current:
                raise Manual("an empty statement")
            inner.append(current)
            current = []
        else:
            current.append(token)
    if current or not inner:
        raise Manual("not the template")
    for statement in inner:
        grant(Cursor(statement), roles, tables)


def role_class(base, candidate, new_tables, *, tables=None, schema="fcs"):
    """`additive` when `candidate` is `base` byte for byte plus appended statements that are comments, GRANTs of
    table privileges to roles `base` already grants to, or the roles.sql guard around such GRANTs for one of
    `new_tables` ({table: columns}, as `new_tables()` returns); else `manual` (ANYM-596 E2). With `tables` (the tables
    that exist), a GRANT target must be one of them or a new table. Standard library only, no I/O, like `classify`."""
    if not candidate.startswith(base):
        return "manual"
    appended = candidate[len(base) :]
    try:
        # The file must scan as its two parts do: the base ends between statements, outside any comment or quote.
        before, added = tokens(base), tokens(appended)
        if tokens(candidate) != before + added or before and before[-1] != ("punct", ";"):
            return "manual"
        if long_name(added):
            return "manual"
        roles, known = granted_roles(base, schema), None if tables is None else set(tables) | set(new_tables)
        for statement in statements(appended, schema):
            c = Cursor(statement)
            if c.peek() == ("word", "grant"):
                grant(c, roles, known)
            elif c.peek() == ("word", "do"):
                guarded(c, roles, known, new_tables, schema)
            else:
                return "manual"
    except (Manual, Untokenizable):
        return "manual"
    return "additive"


def new_tables(text, created=None, schema="fcs"):
    """{table: its column names} for each CREATE TABLE of a migration, with the columns later ADD COLUMNs give
    those tables, added to a copy of `created` (the earlier files' result); `created` alone when it does not parse."""
    found = {table: set(columns) for table, columns in (created or {}).items()}
    try:
        for statement in statements(text, schema):
            c = Cursor(statement)
            if c.word("create") and c.word("table"):
                if c.word("if"):
                    c.expect("not"), c.expect("exists")
                table, columns = c.name(), set()
                for element in split(c.group()):
                    kind, value = element[0] if element else (None, None)
                    if (
                        kind == "ident"
                        or kind == "word"
                        and value
                        not in ("constraint", "primary", "unique", "check", "foreign", "exclude", "like")
                    ):
                        columns.add(value)
                found[table] = columns
            elif c.word("alter") and c.word("table") and (table := c.name()) in found:
                for part in split(statement[c.at :]):
                    p = Cursor(part)
                    if p.word("add") and p.word("column"):
                        found[table].add(p.name())
    except (Manual, Untokenizable):
        return {table: set(columns) for table, columns in (created or {}).items()}
    return found


def plan(base_files, candidate_files, base_roles, candidate_roles, schema="fcs"):
    """K1: the migrate-expand plan of a base..candidate range, or {'pause': <reason>} (ANYM-586).

    `*_files` map version -> schema file text, `*_roles` are roles.sql texts. The range qualifies only when the
    candidate's versions continue 1..N without a gap, every base file is unchanged, each new file is `unattended_class`
    `expand` and roles.sql is `role_class` `additive` with the tables the range creates. The live ledger is compared
    to the base's files where the plan runs (migrate-expand)."""
    versions = sorted(candidate_files)
    if versions != list(range(1, len(versions) + 1)) or not set(base_files) <= set(candidate_files):
        return {"pause": "gap"}
    if sorted(base_files) != list(range(1, len(base_files) + 1)):
        return {"pause": "gap"}
    if any(candidate_files[v] != text for v, text in base_files.items()):
        return {"pause": "edited"}
    base, target = len(base_files), len(versions)
    # The base's tables, as its files create them (Astra r1): an index or a "new" table on an existing name, and a
    # GRANT on anything but a table (a sequence), pause here already; migrate-expand rechecks against the catalog.
    inventory = {"migrations": set()}  # the ledger, which Store.migrate creates outside the files
    for version in range(1, base + 1):
        inventory = new_tables(base_files[version], inventory, schema)
    created = {}
    for version in range(base + 1, target + 1):
        text = candidate_files[version]
        if header_problem(version, text):
            return {"pause": "no header"}
        if unattended_class(text, set(inventory) | set(created), schema) != "expand":
            return {"pause": "not_unattended"}
        created = new_tables(text, created, schema)
    if role_class(base_roles, candidate_roles, created, tables=set(inventory), schema=schema) != "additive":
        return {"pause": "role_not_additive"}
    if base == target and base_roles == candidate_roles:
        return {"pause": "nothing"}
    prefix = base_roles.encode()
    return {
        "base": base,
        "target": target,
        "roles": {"bytes": len(prefix), "sha256": hashlib.sha256(prefix).hexdigest()},
    }
