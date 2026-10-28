"""Rollout class of messaging migrations (ANYM-444): a positive language checked before execution, a header rule,
and a real-PostgreSQL catalog check of every declared `expand`. Synthetic SQL only, in a disposable database."""
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row, tuple_row
from hypothesis import given, settings, strategies as st
import pytest

from messaging_service import migration_rollout as rollout
from messaging_service import store as store_module

SOURCE = Path(store_module.__file__).parent
MIGRATIONS = {int(path.name[:3]): path for path in SOURCE.joinpath('migrations').glob('[0-9][0-9][0-9]_*.sql')}
# The catalog database is built through 035, so 036 is checked against exactly its predecessors.
BASE = 35
SYNTHETIC = f'{rollout.HEADER_FROM:03d}_synthetic.sql'


def source(path):
    """A migration file as deploy reads its blob: UTF-8 bytes, no newline translation."""
    return path.read_bytes().decode()


# Pre-existing objects the synthetic files change: built after the predecessors, before the snapshot.
SETUP = """CREATE TABLE synthetic_existing (id text PRIMARY KEY,
 kind text CONSTRAINT synthetic_existing_kind_check CHECK(kind IN ('a','b')),
 tags text[] CONSTRAINT synthetic_existing_tags_check CHECK(tags <@ ARRAY['a','b']::text[]),
 rank integer CONSTRAINT synthetic_existing_rank_check CHECK(rank IN (-1, 2)),
 "Mode" text CONSTRAINT synthetic_existing_mode_check CHECK("Mode" IN ('x','y')),
 level text CONSTRAINT synthetic_existing_level_check CHECK(level IN ('a')),
 day date CONSTRAINT synthetic_existing_day_check CHECK(day IN ('2026-01-01','2026-01-02')));
CREATE SEQUENCE synthetic_sequence;
INSERT INTO synthetic_existing VALUES ('synthetic-1','b','{a}');"""
EXISTING = {'messages', 'audit_log', 'synthetic_existing'}
EXPAND = '-- rollout: expand\n'
PRECONDITION = """DO $$ BEGIN
 IF NOT (SELECT convalidated FROM pg_constraint
         WHERE conrelid='synthetic_existing'::regclass AND conname='synthetic_existing_kind_check') THEN
  RAISE EXCEPTION 'synthetic_existing_kind_check_not_validated';
 END IF;
END $$;
"""
ALLOWED = {
    'set-local': "SET LOCAL lock_timeout='2s';\nSET LOCAL statement_timeout TO '60000ms';\n"
                 "SET LOCAL lock_timeout=DEFAULT;\n",
    'create-table': """CREATE TABLE synthetic_notes (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 message text NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
 note text NOT NULL DEFAULT '' CHECK(length(note)<=280),
 weight numeric(6,2) DEFAULT -1.5,
 tags text[] NOT NULL DEFAULT '{}'::text[],
 created_at timestamptz NOT NULL DEFAULT now(),
 CONSTRAINT synthetic_notes_unique UNIQUE(message,note),
 CONSTRAINT synthetic_notes_weight CHECK(weight > -100) NO INHERIT,
 FOREIGN KEY(note) REFERENCES synthetic_existing(id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED
);
""",
    'create-index': """CREATE TABLE synthetic_marks (name text, rank integer NOT NULL DEFAULT 0);
CREATE UNIQUE INDEX synthetic_marks_name ON synthetic_marks(name);
CREATE INDEX ON synthetic_marks USING btree (name DESC, "rank");
""",
    'add-column': """ALTER TABLE messages ADD COLUMN synthetic_note text;
ALTER TABLE messages ADD COLUMN synthetic_rank integer NOT NULL DEFAULT 0,
 ADD COLUMN synthetic_label text NULL DEFAULT 'none'::text;
""",
    'widen-in': """ALTER TABLE synthetic_existing DROP CONSTRAINT synthetic_existing_kind_check;
ALTER TABLE synthetic_existing ADD CONSTRAINT synthetic_existing_kind_check CHECK(kind IN ('a','b','c')) NOT VALID;
""",
    'widen-array': """ALTER TABLE synthetic_existing DROP CONSTRAINT synthetic_existing_tags_check;
ALTER TABLE synthetic_existing ADD CONSTRAINT synthetic_existing_tags_check
 CHECK (tags <@ ARRAY['a','b','c']::text[]) NOT VALID;
""",
    'widen-numeric': """ALTER TABLE synthetic_existing DROP CONSTRAINT synthetic_existing_rank_check;
ALTER TABLE synthetic_existing ADD CONSTRAINT synthetic_existing_rank_check CHECK(rank IN (2, -1, 3)) NOT VALID;
""",
    'widen-quoted-column': """ALTER TABLE synthetic_existing DROP CONSTRAINT synthetic_existing_mode_check;
ALTER TABLE synthetic_existing ADD CONSTRAINT synthetic_existing_mode_check CHECK("Mode" IN ('x','y','z')) NOT VALID;
""",
    'widen-singleton': """ALTER TABLE synthetic_existing DROP CONSTRAINT synthetic_existing_level_check;
ALTER TABLE synthetic_existing ADD CONSTRAINT synthetic_existing_level_check CHECK(level IN ('a','b')) NOT VALID;
""",
    'widen-date': """ALTER TABLE synthetic_existing DROP CONSTRAINT synthetic_existing_day_check;
ALTER TABLE synthetic_existing ADD CONSTRAINT synthetic_existing_day_check
 CHECK(day IN ('2026-01-01','2026-01-02','2026-01-03')) NOT VALID;
""",
    'precondition': PRECONDITION,
    'comment': "COMMENT ON TABLE messages IS 'synthetic';\nCOMMENT ON COLUMN messages.body IS 'synthetic body';\n",
}
# id: (file, what classify says, whether the catalog check alone also rejects it); None: never applied.
COUNTEREXAMPLES = {
    'commit-after-column': ("ALTER TABLE messages ADD COLUMN synthetic_note text;\nCOMMIT;\n", 'manual', None),
    'setval': ("SELECT setval('synthetic_sequence', 1000);\n", 'manual', True),
    'data-dependent-do': ("DO $$ BEGIN IF (SELECT count(*) FROM synthetic_existing) > 0 THEN\n"
                          " UPDATE synthetic_existing SET kind='a'; END IF; END $$;\n", 'manual', True),
    'trigger': ("CREATE TRIGGER synthetic_trigger BEFORE UPDATE ON messages FOR EACH ROW\n"
                " EXECUTE FUNCTION suppress_redundant_updates_trigger();\n", 'manual', True),
    'execute': ("DO $$ BEGIN EXECUTE 'CREATE INDEX synthetic_by_sender ON messages(sender)'; END $$;\n",
                'manual', True),
    'narrowed-list': ("ALTER TABLE synthetic_existing DROP CONSTRAINT synthetic_existing_kind_check;\n"
                      "ALTER TABLE synthetic_existing ADD CONSTRAINT synthetic_existing_kind_check\n"
                      " CHECK(kind IN ('a','c')) NOT VALID;\n", 'expand', True),
    'widening-without-not-valid': ("ALTER TABLE synthetic_existing DROP CONSTRAINT synthetic_existing_kind_check;\n"
                                   "ALTER TABLE synthetic_existing ADD CONSTRAINT synthetic_existing_kind_check\n"
                                   " CHECK(kind IN ('a','b','c'));\n", 'manual', True),
    'index-on-messages': ("CREATE INDEX synthetic_by_sender ON messages(sender);\n", 'manual', True),
    'insert': ("INSERT INTO synthetic_existing VALUES ('synthetic-2','a','{}');\n", 'manual', True),
    'function': ("CREATE FUNCTION public.synthetic_one() RETURNS integer LANGUAGE sql AS $$ SELECT 1 $$;\n",
                 'manual', True),
    'row-level-security': ("ALTER TABLE messages ENABLE ROW LEVEL SECURITY;\n", 'manual', True),
    'force-row-level-security': ("ALTER TABLE messages FORCE ROW LEVEL SECURITY;\n", 'manual', True),
    'narrowed-numeric': (ALLOWED['widen-numeric'].replace('(2, -1, 3)', '(-1, 3)'), 'expand', True),
    'narrowed-quoted-column': (ALLOWED['widen-quoted-column'].replace("('x','y','z')", "('x','z')"), 'expand', True),
    'narrowed-singleton': (ALLOWED['widen-singleton'].replace("('a','b')", "('b')"), 'expand', True),
    'narrowed-date': (ALLOWED['widen-date'].replace("('2026-01-01','2026-01-02',", "('2026-01-02',"), 'expand',
                      True),
    'widened-other-column': (ALLOWED['widen-singleton'].replace('(level IN', '("Mode" IN'), 'expand', True),
}
# PostgreSQL continues this E-string across the escaped newline and the escaped quote, closes it after b and runs the
# ALTER (Astra r1); a lexer without the escaped newline sees a string up to the quote in the trailing comment.
HIDDEN_RLS = "ALTER TABLE messages ENABLE ROW LEVEL SECURITY;"
HIDDEN = ("CREATE TABLE synthetic_t(x text);\nCREATE INDEX synthetic_i ON synthetic_t ((E'a\\\n\\'b'::text));\n"
          + HIDDEN_RLS + " -- '\n")
# More text `classify` must call manual: transaction control, other changes, and what it cannot tokenize.
MANUAL = {
    'begin': "BEGIN;\nALTER TABLE messages ADD COLUMN synthetic_note text;",
    'end': "ALTER TABLE messages ADD COLUMN synthetic_note text;\nEND;",
    'rollback': "ROLLBACK;",
    'savepoint': "SAVEPOINT synthetic;",
    'select': "SELECT 1;",
    'call': "CALL synthetic_procedure();",
    'prepared-execute': "EXECUTE synthetic_plan;",
    'update': "UPDATE messages SET body='x';",
    'delete': "DELETE FROM messages;",
    'truncate': "TRUNCATE synthetic_existing;",
    'grant': "GRANT SELECT ON messages TO PUBLIC;",
    'role': "CREATE ROLE synthetic_role;",
    'drop-table': "DROP TABLE synthetic_existing;",
    'drop-column': "ALTER TABLE messages DROP COLUMN body;",
    'rename': "ALTER TABLE messages RENAME TO synthetic_messages;",
    'type-change': "ALTER TABLE messages ALTER COLUMN body TYPE varchar(10);",
    'set-not-null': "ALTER TABLE messages ALTER COLUMN body SET NOT NULL;",
    'set-default': "ALTER TABLE messages ALTER COLUMN body SET DEFAULT '';",
    'constraint-on-existing': "ALTER TABLE messages ADD CONSTRAINT synthetic_check CHECK(body<>'') NOT VALID;",
    'unique-index-on-existing': "CREATE UNIQUE INDEX synthetic_by_id ON messages(id);",
    'not-null-without-default': "ALTER TABLE messages ADD COLUMN synthetic_rank integer NOT NULL;",
    'not-null-null-default': "ALTER TABLE messages ADD COLUMN synthetic_rank integer NOT NULL DEFAULT NULL;",
    'column-with-check': "ALTER TABLE messages ADD COLUMN synthetic_rank integer DEFAULT 0 CHECK(synthetic_rank>=0);",
    'column-with-reference': "ALTER TABLE messages ADD COLUMN synthetic_chat text REFERENCES chats(id);",
    'column-function-default': "ALTER TABLE messages ADD COLUMN synthetic_at timestamptz DEFAULT now();",
    'generated-column': "ALTER TABLE messages ADD COLUMN synthetic_len integer GENERATED ALWAYS AS (length(body)) "
                        "STORED;",
    'identity-column': "ALTER TABLE messages ADD COLUMN synthetic_id bigint GENERATED ALWAYS AS IDENTITY;",
    'serial-column': "ALTER TABLE messages ADD COLUMN synthetic_id bigserial;",
    'column-on-unknown-table': "ALTER TABLE synthetic_missing ADD COLUMN synthetic_note text;",
    'new-table-function-default': "CREATE TABLE synthetic_t (n double precision DEFAULT random());",
    'new-table-generated': "CREATE TABLE synthetic_t (n integer GENERATED ALWAYS AS IDENTITY);",
    'new-table-serial': "CREATE TABLE synthetic_t (n serial);",
    'new-table-like': "CREATE TABLE synthetic_t (LIKE messages);",
    'new-table-as-select': "CREATE TABLE synthetic_t AS SELECT * FROM messages;",
    'existing-table-created': "CREATE TABLE IF NOT EXISTS messages (id text);",
    'zero-timeout': "SET LOCAL lock_timeout='0';",
    'zero-ms-timeout': "SET LOCAL statement_timeout='0ms';",
    'long-lock-timeout': "SET LOCAL lock_timeout='6s';",
    'number-timeout': "SET LOCAL lock_timeout=2000;",
    'session-set': "SET lock_timeout='2s';",
    'other-setting': "SET LOCAL search_path='synthetic';",
    'template-with-update': PRECONDITION.replace("END IF;", "END IF; UPDATE messages SET body='x';"),
    'drop-without-add': "ALTER TABLE synthetic_existing DROP CONSTRAINT synthetic_existing_kind_check;",
    'pair-other-name': "ALTER TABLE synthetic_existing DROP CONSTRAINT synthetic_existing_kind_check;\n"
                       "ALTER TABLE synthetic_existing ADD CONSTRAINT other_check CHECK(kind IN ('a')) NOT VALID;",
    'pair-expression': "ALTER TABLE synthetic_existing DROP CONSTRAINT synthetic_existing_kind_check;\n"
                       "ALTER TABLE synthetic_existing ADD CONSTRAINT synthetic_existing_kind_check "
                       "CHECK(kind IN (lower('A'))) NOT VALID;",
    'unterminated-string': "COMMENT ON TABLE messages IS 'synthetic;",
    'unterminated-dollar': "DO $x$ BEGIN END $$;",
    'unterminated-comment': "/* synthetic /* nested */ COMMENT ON TABLE messages IS 'x';",
    'parameter': "SET LOCAL lock_timeout=$1;",
    'psql-meta': "\\i synthetic.sql",
    'psql-variable': "SET LOCAL lock_timeout=:timeout;",
    'clock-timestamp-default': "CREATE TABLE synthetic_t (seen_at timestamptz DEFAULT clock_timestamp());",
    'statement-timeout-default': "SET LOCAL statement_timeout=DEFAULT;",
    'index-expression': "CREATE TABLE synthetic_t (name text);\nCREATE INDEX ON synthetic_t (lower(name));",
    'index-where': "CREATE TABLE synthetic_t (name text);\nCREATE INDEX ON synthetic_t (name) WHERE name <> '';",
    'index-include': "CREATE TABLE synthetic_t (name text, n int);\nCREATE INDEX ON synthetic_t (name) INCLUDE (n);",
    'index-with': "CREATE TABLE synthetic_t (name text);\nCREATE INDEX ON synthetic_t (name) WITH (fillfactor=50);",
    'index-opclass': "CREATE TABLE synthetic_t (name text);\nCREATE INDEX ON synthetic_t (name text_pattern_ops);",
    'index-nulls': "CREATE TABLE synthetic_t (name text);\nCREATE INDEX ON synthetic_t (name NULLS FIRST);",
    'index-method': "CREATE TABLE synthetic_t (name text);\nCREATE INDEX ON synthetic_t USING hash (name);",
    'index-concurrently': "CREATE TABLE synthetic_t (name text);\nCREATE INDEX CONCURRENTLY ON synthetic_t (name);",
    'table-exclude': "CREATE TABLE synthetic_t (n int, EXCLUDE USING btree (n WITH =));",
    'table-unique-include': "CREATE TABLE synthetic_t (n int, m int, UNIQUE (n) INCLUDE (m));",
    'table-constraint-tail': "CREATE TABLE synthetic_t (n int, PRIMARY KEY (n) WITH (fillfactor=50));",
    # Lexing as PostgreSQL does: these hide a statement, or are forms the language leaves out.
    'escape-string-hides-a-statement': EXPAND + HIDDEN,
    'escape-string-hides-a-select': EXPAND + HIDDEN.replace(HIDDEN_RLS, 'SELECT 1;'),
    'carriage-return-ends-a-comment': "ALTER TABLE messages ADD COLUMN synthetic_note text; -- c\rSELECT 1;",
    'nested-comment-still-open': "/* a /* b */ COMMENT ON TABLE messages IS 'x'; */ SELECT 1;",
    'string-continued': "COMMENT ON TABLE messages IS 'a'\n'b';",
    'escape-string-continued': "COMMENT ON TABLE messages IS E'a' -- c\n'\\'; SELECT 1; --';",
    'unicode-string': "COMMENT ON TABLE messages IS U&'d\\0061ta';",
    'unicode-identifier': "COMMENT ON TABLE U&\"m\\0065ssages\" IS 'x';",
    'bit-string': "ALTER TABLE messages ADD COLUMN synthetic_bits bit(2) DEFAULT B'01';",
    'hex-string': "ALTER TABLE messages ADD COLUMN synthetic_bits bit(8) DEFAULT X'1F';",
    'national-string': "COMMENT ON TABLE messages IS N'x';",
    'number-junk': "ALTER TABLE messages ADD COLUMN synthetic_rank integer DEFAULT 0x10;",
    'unicode-space': "COMMENT ON TABLE messages IS 'x';",
    'vertical-tab': "COMMENT ON TABLE messages\vIS 'x';",
    'zero-length-identifier': 'COMMENT ON TABLE "" IS \'x\';',
    'tagged-dollar-quote': "DO $a$ BEGIN PERFORM 1; $$ END $$; $a$;",
}


def tables(c):
    return {row["relname"] for row in c.execute("""SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind IN ('r','p')""")}


def snapshot(c):
    """The catalog state the catalog check compares (comments excluded: COMMENT ON is allowed)."""
    def rows(query):
        with c.cursor(row_factory=tuple_row) as cursor:
            return cursor.execute(query).fetchall()
    public = "JOIN pg_namespace n ON n.oid=c.relnamespace AND n.nspname='public'"
    return {
        'relations': {row[0]: row[1:] for row in rows(f"""SELECT c.relname, c.relkind, c.relacl::text, c.relowner,
            c.reloptions::text, c.relfilenode, c.relrowsecurity, c.relforcerowsecurity, c.relpersistence,
            c.relreplident, t.relname FROM pg_class c {public}
            LEFT JOIN pg_index x ON x.indexrelid=c.oid LEFT JOIN pg_class t ON t.oid=x.indrelid""")},
        'columns': {row[:2]: row[2:] for row in rows(f"""SELECT c.relname, a.attname, format_type(a.atttypid,a.atttypmod),
            a.attnotnull, pg_get_expr(d.adbin,d.adrelid), a.attgenerated, a.attidentity, a.attacl::text
            FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid {public}
            LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
            WHERE a.attnum>0 AND NOT a.attisdropped AND c.relkind IN ('r','p','v','m','f')""")},
        'constraints': {row[:2]: row[2:] for row in rows(f"""SELECT c.relname, k.conname, k.contype,
            pg_get_constraintdef(k.oid), k.convalidated FROM pg_constraint k JOIN pg_class c ON c.oid=k.conrelid
            {public}""")},
        'dml': {row[0]: row[1] for row in rows("""SELECT relname, n_tup_ins+n_tup_upd+n_tup_del
            FROM pg_stat_xact_user_tables WHERE schemaname='public'""")},
        'triggers': set(rows("SELECT tgrelid::regclass::text, tgname, pg_get_triggerdef(oid) FROM pg_trigger "
                             "WHERE NOT tgisinternal")),
        'functions': set(rows("""SELECT p.oid::regprocedure::text, md5(p.prosrc), p.proacl::text, p.proowner,
            p.prosecdef, p.proconfig::text FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname NOT IN ('pg_catalog','information_schema')""")),
        'roles': set(rows("""SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolcanlogin,
            rolreplication, rolbypassrls, rolconnlimit, rolvaliduntil::text, rolconfig::text FROM pg_roles""")),
        'memberships': set(rows("SELECT roleid::regrole::text, member::regrole::text, admin_option FROM pg_auth_members")),
        'policies': set(rows("SELECT polrelid::regclass::text, polname, polcmd, polroles::text FROM pg_policy")),
        'rules': set(rows("SELECT ev_class::regclass::text, rulename FROM pg_rewrite WHERE rulename<>'_RETURN'")),
        'types': set(rows("""SELECT t.typname, t.typtype, t.typacl::text FROM pg_type t
            JOIN pg_namespace n ON n.oid=t.typnamespace AND n.nspname='public'
            WHERE t.typrelid=0 AND t.typelem=0""")),
        'enums': set(rows("SELECT enumtypid::regtype::text, enumlabel, enumsortorder::text FROM pg_enum")),
        'namespaces': set(rows("SELECT nspname, nspowner, nspacl::text FROM pg_namespace")),
        'default_privileges': set(rows("SELECT defaclrole, defaclnamespace, defaclobjtype, defaclacl::text "
                                       "FROM pg_default_acl")),
        'extensions': set(rows("SELECT extname, extversion FROM pg_extension")),
        'sequences': set(rows("SELECT sequencename, last_value FROM pg_sequences")),
    }


NUMERIC_TYPES = {'smallint', 'integer', 'bigint', 'numeric', 'real', 'double precision'}
TEXT_TYPES = {'text', 'character varying', 'character', 'bpchar', 'name'}


def cast_type(c):
    """The type after `::`, its words joined and `[]` for an array; a type modifier is dropped."""
    words = []
    while c.peek()[0] in ('word', 'ident'): words.append(c.next()[1])
    if not words: raise rollout.Manual('no type')
    if c.peek() == ('punct', '('): c.group()
    array = c.peek() == ('punct', '[')
    if array and c.group('[', ']'): raise rollout.Manual('array bound')
    return ' '.join(words) + '[]' * array


def operand(c):
    """(text, kind, type) of a literal as pg_get_constraintdef prints it: 2, 1.5, 'a'::text, '-1'::integer,
    (1)::bigint or ('-2'::integer)::numeric; the outermost cast is its type."""
    if c.take('punct', '('):
        text, kind, type_ = operand(c)
        if not c.take('punct', ')'): raise rollout.Manual('unbalanced')
    else:
        negative = c.take('op', '-')
        kind, text = c.next()
        if kind not in ('string', 'number') or negative and kind != 'number': raise rollout.Manual('not a literal')
        text, type_ = ('-' if negative else '') + text, None
    while c.take('cast', '::'): type_ = cast_type(c)
    return text, kind, type_


def array(c):
    """The operands of `ARRAY[…]` or `(ARRAY[…])::type[]`; an outer cast types every element."""
    if c.take('punct', '('):
        items = array(c)
        if not c.take('punct', ')'): raise rollout.Manual('unbalanced')
    else:
        c.expect('array')
        items = []
        for part in rollout.split(c.group('[', ']')):
            element = rollout.Cursor(part)
            items.append(operand(element))
            element.end()
    while c.take('cast', '::'):
        type_ = cast_type(c)
        if not type_.endswith('[]'): raise rollout.Manual('not an array type')
        items = [(text, kind, type_[:-2]) for text, kind, _ in items]
    return items


def value(text, kind, type_):
    """A literal's value as the column compares it: a Decimal for a numeric type, the string for a text type, and
    for any other type (date, ...) the type with the canonical text pg_get_constraintdef prints for the stored value."""
    type_ = type_ or ('numeric' if kind == 'number' else None)
    if type_ in NUMERIC_TYPES: return ('number', Decimal(text))
    if type_ in TEXT_TYPES and kind == 'string': return ('text', text)
    if type_ and kind == 'string' and not type_.endswith('[]'): return (type_, text)
    raise rollout.Manual('a literal the catalog check cannot compare')


def column(c):
    """A column reference with its casts: `kind`, `"Mode"`, `(v)::text`."""
    if c.take('punct', '('):
        name = column(c)
        if not c.take('punct', ')'): raise rollout.Manual('unbalanced')
    else:
        kind, text = c.next()
        if kind not in ('word', 'ident'): raise rollout.Manual('not a column')
        name = (text,)
    while c.take('cast', '::'): name += (cast_type(c),)
    return name


def values(definition):
    """(column, form, values) of a pg_get_constraintdef value list, or None: `CHECK ((c = ANY (ARRAY[…])))`, the
    singleton `CHECK ((c = 'a'::text))` (form `any`, one value) or `CHECK ((c <@ ARRAY[…]))`, optionally NOT VALID."""
    try:
        c = rollout.Cursor(rollout.tokens(definition))
        c.expect('check')
        outer = rollout.Cursor(c.group())
        if c.word('not'): c.expect('valid')
        c.end()
        e = rollout.Cursor(outer.group())
        outer.end()
        name = column(e)
        if e.take('op', '<@'): form, items = 'contained', array(e)
        elif not e.take('op', '='): return None
        elif e.word('any'):
            inner = rollout.Cursor(e.group())
            form, items = 'any', array(inner)
            inner.end()
        else: form, items = 'any', [operand(e)]
        e.end()
        return name, form, {value(*item) for item in items}
    except (rollout.Manual, rollout.Untokenizable, ArithmeticError):
        return None


def catalog_problems(before, after):
    """What changed on pre-existing objects beyond the allowed kinds: new nullable or literal-default columns, the
    NOT NULL constraints of those, a CHECK value list widened to a superset NOT VALID, and comments."""
    problems = []
    old_tables = {name for name, row in before['relations'].items() if row[0] in ('r', 'p')}
    for name, row in before['relations'].items():
        if after['relations'].get(name) != row: problems.append(f'relation {name} changed or was rewritten')
    for name, (kind, acl, *_, table) in after['relations'].items():
        if name in before['relations'] or kind == 'r' and acl is None or kind == 'i' and table not in old_tables:
            continue
        problems.append(f'new relation {name} ({kind}{" on " + table if table else ""})')
    new_columns = set()
    for key, row in after['columns'].items():
        if key in before['columns']:
            if before['columns'][key] != row: problems.append(f'column {key} changed')
        elif key[0] in old_tables:
            new_columns.add(key)
            _, not_null, default, generated, identity, acl = row
            if generated or identity or acl or not_null and default is None:
                problems.append(f'new column {key} is generated, identity, granted or NOT NULL without default')
    problems += [f'column {key} was dropped' for key in before['columns'] if key not in after['columns']]
    for key, row in before['constraints'].items():
        if after['constraints'].get(key) == row: continue
        old, new = values(row[1]), key in after['constraints'] and values(after['constraints'][key][1])
        if not (old and new and old[:2] == new[:2] and old[2] <= new[2] and after['constraints'][key][2] is False):
            problems.append(f'constraint {key} changed other than a NOT VALID widening')
    for key, (kind, definition, _) in after['constraints'].items():
        if key not in before['constraints'] and key[0] in old_tables and not (
                kind == 'n' and (key[0], definition.removeprefix('NOT NULL ').strip('"')) in new_columns):
            problems.append(f'new constraint {key} on an existing table')
    problems += [f'rows of {name} changed' for name in old_tables
                 if after['dml'].get(name, 0) != before['dml'].get(name, 0)]
    problems += [f'{kind} changed' for kind in ('triggers', 'functions', 'roles', 'memberships', 'policies', 'rules',
                                               'types', 'enums', 'namespaces', 'default_privileges', 'extensions',
                                               'sequences') if before[kind] != after[kind]]
    return problems


@pytest.fixture(scope='module')
def catalog(database):
    """A disposable database at schema 35; each check runs in one transaction that it rolls back."""
    name = 'anymsg_rollout_' + uuid4().hex + '_test'
    dsn = database.pool.conninfo
    with psycopg.connect(dsn, autocommit=True) as owner:
        owner.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    try:
        with psycopg.connect(make_conninfo(dsn, dbname=name), row_factory=dict_row) as c:
            c.execute(SOURCE.joinpath('schema.sql').read_text())
            for version in sorted(v for v in MIGRATIONS if v <= BASE):
                c.execute(source(MIGRATIONS[version]))
                if version == 17:
                    from messaging_service.chat_evidence import backfill
                    backfill(c)
            c.commit()
            yield c
    finally:
        with psycopg.connect(dsn, autocommit=True) as owner:
            owner.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))


def prepare(c, upto, setup):
    """The migrations after BASE through `upto`, then `setup`, inside the check's transaction."""
    for version in sorted(v for v in MIGRATIONS if BASE < v <= upto):
        c.execute(source(MIGRATIONS[version]))
    if setup: c.execute(setup)


def check(c, text, *, upto, setup=''):
    """`classify` of `text` with the real table inventory after the migrations through `upto` and `setup` and, only
    for `expand`, the catalog problems of applying it. A `manual` text is never executed: (verdict, None)."""
    try:
        prepare(c, upto, setup)
        verdict = rollout.classify(text, tables(c))
        if verdict != 'expand': return verdict, None
        before = snapshot(c)
        c.execute(text)
        return verdict, catalog_problems(before, snapshot(c))
    finally:
        c.rollback()


def catalog_only(c, text, *, upto, setup=''):
    """The catalog check on its own: the problems of deliberately applying synthetic `text`, never classified."""
    try:
        prepare(c, upto, setup)
        before = snapshot(c)
        c.execute(text)
        return catalog_problems(before, snapshot(c))
    finally:
        c.rollback()


def rollout_failures(c, name, text, *, setup=''):
    """What fails CI for migration file `name`: the header rule and, for a declared expand, classify (before any
    execution) and then the catalog check."""
    if problem := rollout.header_problem(name, text): return [problem]
    if rollout.header(text) != 'expand': return []
    verdict, problems = check(c, text, upto=int(name[:3]) - 1, setup=setup)
    return problems if verdict == 'expand' else [f'{name}: classify says manual']


class Recording:
    """The catalog connection, recording every text sent to `execute`."""
    def __init__(self, connection):
        self.connection, self.executed = connection, []

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, query, *args, **kwargs):
        self.executed.append(query)
        return self.connection.execute(query, *args, **kwargs)


# spec: ANYM-444
@pytest.mark.parametrize('form', ALLOWED)
def test_each_allowed_form_classifies_expand_and_passes_the_catalog_check(catalog, form):
    text = EXPAND + ALLOWED[form]
    assert rollout.classify(text, EXISTING) == rollout.classify(text) == 'expand'
    assert rollout_failures(catalog, SYNTHETIC, text, setup=SETUP) == []


# spec: ANYM-444
@pytest.mark.parametrize('example', COUNTEREXAMPLES)
def test_a_declared_expand_counterexample_fails_the_rollout_check(catalog, example):
    text, verdict, catalog_rejects = COUNTEREXAMPLES[example]
    assert rollout.classify(EXPAND + text, EXISTING) == verdict
    assert rollout_failures(catalog, SYNTHETIC, EXPAND + text, setup=SETUP)
    if catalog_rejects:
        # The catalog check rejects it on its own, independent of classify.
        assert catalog_only(catalog, text, upto=rollout.HEADER_FROM - 1, setup=SETUP)


# spec: ANYM-444
@pytest.mark.parametrize('example', MANUAL)
def test_classify_calls_everything_outside_the_language_manual(example):
    assert rollout.classify(MANUAL[example], EXISTING) == 'manual'
    assert rollout.rollout_class(EXPAND + MANUAL[example], EXISTING) == 'manual'


# spec: ANYM-444
def test_quotes_comments_and_dollar_quotes_hide_nothing_from_the_statement_split():
    text = EXPAND + """/* COMMIT; /* nested; */ still a comment */
-- COMMIT;
COMMENT ON TABLE messages IS 'COMMIT; it''s quoted';
COMMENT ON COLUMN "messages"."body" IS E'a \\' COMMIT;';
""" + PRECONDITION.replace("'synthetic_existing_kind_check_not_validated'", "'BEGIN; COMMIT;'")
    assert rollout.classify(text, EXISTING) == 'expand'
    assert rollout.classify(text.replace("IS 'COMMIT;", "IS 'COMMIT';COMMIT;'"), EXISTING) == 'manual'
    assert rollout.classify(text + "\nDO $x$ BEGIN END $x$;", EXISTING) == 'manual'


# spec: ANYM-444
def test_statement_boundaries_are_the_ones_postgresql_scans():
    split = rollout.statements("SELECT $a$ ; $$ ; $a$; SELECT ';'; SELECT \"a;b\"; SELECT E'\\';\\\n';"
                               " /* ; /* ; */ ; */ SELECT 1 -- ;\r; SELECT e'x' -- c\n;")
    assert [len(statement) for statement in split] == [2, 2, 2, 2, 2, 2]
    assert split[0][1] == ('dollar', ' ; $$ ; ') and split[3][1] == ('estring', "\\';\\\n")
    assert rollout.classify(EXPAND + "/* outer /* inner */ COMMIT; */ COMMENT ON TABLE messages IS E'a\\\nb';\n",
                            EXISTING) == 'expand'


# spec: ANYM-444
@pytest.mark.parametrize('body', [HIDDEN, HIDDEN.replace(HIDDEN_RLS, 'SELECT 1;')], ids=['row-security', 'select'])
def test_an_escape_string_continued_across_a_newline_hides_no_statement_from_classify(catalog, body):
    assert rollout.classify(EXPAND + body, EXISTING) == rollout.classify(EXPAND + body) == 'manual'
    try:
        # PostgreSQL runs the statement after the E-string: the ALTER turns on row security of messages.
        catalog.execute(body)
        row = catalog.execute("SELECT relrowsecurity FROM pg_class WHERE relname='messages'").fetchone()
        assert row['relrowsecurity'] is (body == HIDDEN)
    finally:
        catalog.rollback()
    if body == HIDDEN:
        assert 'relation messages changed or was rewritten' in catalog_only(catalog, body, upto=rollout.HEADER_FROM - 1)


# spec: ANYM-444
@pytest.mark.parametrize('body', [COUNTEREXAMPLES['commit-after-column'][0], HIDDEN], ids=['commit', 'hidden'])
def test_a_declared_expand_file_that_classify_rejects_never_reaches_execute(catalog, body):
    recording, text = Recording(catalog), EXPAND + body
    assert rollout_failures(recording, SYNTHETIC, text, setup=SETUP) == [f'{SYNTHETIC}: classify says manual']
    assert SETUP in recording.executed and not [query for query in recording.executed if body in str(query)]
    assert catalog.execute("SELECT to_regclass('synthetic_existing') IS NULL AS gone").fetchone()['gone']
    catalog.rollback()


# spec: ANYM-444
def test_036_classifies_expand_and_passes_the_catalog_check_after_035(catalog):
    text = source(MIGRATIONS[36])
    assert rollout.classify(text) == 'expand'
    assert check(catalog, text, upto=BASE) == ('expand', [])


# spec: ANYM-444
def test_applied_migrations_need_no_header_and_every_later_one_declares_its_class():
    assert rollout.HEADER_FROM == 41 and 40 in MIGRATIONS
    for version, path in MIGRATIONS.items():
        text = source(path)
        assert rollout.classify(text) in ('expand', 'manual')  # terminates on every real file, as the pause needs
        if version < rollout.HEADER_FROM:
            assert rollout.header_problem(path.name, text) is None
    body = 'ALTER TABLE messages ADD COLUMN synthetic_note text;\n'
    assert rollout.header_problem('040_synthetic.sql', body) is None
    assert 'must start with' in rollout.header_problem(SYNTHETIC, body)
    assert 'must start with' in rollout.header_problem('042_synthetic.sql', ' ' + EXPAND + body)
    assert rollout.header_problem(SYNTHETIC, EXPAND + body) is None
    assert rollout.header_problem(SYNTHETIC, '-- rollout: manual\n' + body) is None


# spec: ANYM-444
def test_every_migration_from_041_passes_the_header_rule_and_a_declared_expand_both_checks(catalog):
    for version in sorted(v for v in MIGRATIONS if v >= rollout.HEADER_FROM):
        path = MIGRATIONS[version]
        assert rollout_failures(catalog, path.name, source(path)) == []
    # The same check fails a file that breaks the rule or claims expand for a non-expand body.
    assert rollout_failures(catalog, SYNTHETIC, ALLOWED['add-column'])
    assert rollout_failures(catalog, SYNTHETIC, EXPAND + "INSERT INTO messages_synthetic VALUES (1);")


# spec: ANYM-444
def test_the_pause_class_is_no_header_a_declared_manual_or_classify_of_a_declared_expand():
    body = ALLOWED['add-column']
    assert rollout.rollout_class(body) == 'no header'
    assert rollout.rollout_class('-- rollout: manual\n' + body) == 'manual'
    assert rollout.rollout_class(EXPAND + body) == 'expand'
    assert rollout.rollout_class(EXPAND + COUNTEREXAMPLES['insert'][0]) == 'manual'


# 038 holds the widening pair too, but its partial unique index already makes it manual for `classify`.
# spec: ANYM-596
@pytest.mark.parametrize(('body', 'pause_class'), [(ALLOWED['widen-in'], 'expand'), (source(MIGRATIONS[36]), 'expand'),
                                                   (source(MIGRATIONS[38]), 'manual')], ids=['widening-pair', '036', '038'])
def test_the_unattended_class_calls_any_drop_manual_while_the_rollout_class_stays_expand(body, pause_class):
    text = EXPAND + body
    assert rollout.rollout_class(text) == pause_class
    assert rollout.unattended_class(text) == 'manual'
    assert rollout.unattended_class(text, EXISTING) == 'manual'


# spec: ANYM-596
@pytest.mark.parametrize('form', [f for f in ALLOWED if not f.startswith('widen')])
def test_the_unattended_class_keeps_every_drop_free_expand_form(form):
    assert rollout.unattended_class(EXPAND + ALLOWED[form], EXISTING) == 'expand'
    assert rollout.unattended_class(ALLOWED[form], EXISTING) == 'manual'  # no header
    assert rollout.unattended_class('-- rollout: manual\n' + ALLOWED[form], EXISTING) == 'manual'


# The appended text of each role file in #323 (schema 49) and #326 (schema 50), as merged.
ROLE_049 = {
    'gateway_role.sql': """-- Schema 49 (ANYM-519): self-service endpoints. Header values and signing secrets are write-only here:
-- INSERT and UPDATE, never SELECT (no RETURNING or SET reads them either); only the dispatcher reads them.
-- signing_secret UPDATE exists only to clear it on delete. Privilege separation, not encryption.
GRANT SELECT(id,principal,key_id,request_id,request_hash,url,host,label,header_names,headers_fingerprint,
    secret_fingerprint,pending_fingerprint,rotation_started_at,state,version,created_at,updated_at)
    ON delivery_endpoints TO messaging_gateway;
GRANT INSERT(principal,key_id,request_id,request_hash,url,host,label,header_names,headers_fingerprint,
    secret_fingerprint,header_values,signing_secret) ON delivery_endpoints TO messaging_gateway;
GRANT UPDATE(header_names,headers_fingerprint,header_values,pending_secret,pending_fingerprint,rotation_started_at,
    signing_secret,state,version,updated_at) ON delivery_endpoints TO messaging_gateway;
GRANT SELECT,INSERT ON delivery_endpoint_tests TO messaging_gateway;
GRANT UPDATE(state,finished_at,error_code) ON delivery_endpoint_tests TO messaging_gateway;
""",
    'subscription_role.sql': """-- Schema 49 (ANYM-519): an endpoint binding stages a hint only while its endpoint is enabled; no
-- secret, header value or URL column.
GRANT SELECT(id,state) ON delivery_endpoints TO messaging_subscriptions;
""",
    'delivery_role.sql': """-- Schema 49 (ANYM-519): the only reader of endpoint secrets and header values; it promotes a rotation
-- after 24 h, records test results and reads the operator's activation (delivery_features).
GRANT SELECT ON probe_metadata TO messaging_event_delivery;
GRANT SELECT ON delivery_endpoints TO messaging_event_delivery;
GRANT UPDATE(signing_secret,pending_secret,secret_fingerprint,pending_fingerprint,rotation_started_at,updated_at)
    ON delivery_endpoints TO messaging_event_delivery;
GRANT SELECT ON delivery_endpoint_tests TO messaging_event_delivery;
GRANT SELECT(subscription,acked_through) ON subscription_consumption TO messaging_event_delivery;
-- References without message text: IDs, timing and native wake evidence only (049).
GRANT EXECUTE ON FUNCTION public.subscription_hint_references(uuid,bigint,bigint,text[],text[]) TO messaging_event_delivery;
GRANT UPDATE(state,lease_token,lease_until,http_status,latency_ms,error_code,finished_at)
    ON delivery_endpoint_tests TO messaging_event_delivery;
""",
}
BEAT = """-- Schema 50 (ANYM-534): only this component's heartbeat row (row-level security).
GRANT SELECT,INSERT,UPDATE ON component_heartbeats TO {role};
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_policies WHERE tablename='component_heartbeats' AND policyname='{role}_beat') THEN
        CREATE POLICY {role}_beat ON component_heartbeats TO {role} USING (component='{component}') WITH CHECK (component='{component}');
    END IF;
END $$;
"""
ROLE_050 = {'gateway_role.sql': BEAT.format(role='messaging_gateway', component='api'),
            'subscription_role.sql': BEAT.format(role='messaging_subscriptions', component='matcher'),
            'delivery_role.sql': BEAT.format(role='messaging_event_delivery', component='dispatcher')}
HEARTBEATS = rollout.new_tables(source(MIGRATIONS[50]))  # {component_heartbeats: its four columns}


def role_base(name, block):
    """The role file as it stood before `block` was appended: the current file up to the block."""
    text = SOURCE.joinpath(name).read_text()
    assert block in text
    return text[:text.index(block)]


# spec: ANYM-596
@pytest.mark.parametrize(('name', 'block', 'new_tables', 'expected'), [
    ('gateway_role.sql', ROLE_049['gateway_role.sql'], {}, 'additive'),
    ('subscription_role.sql', ROLE_049['subscription_role.sql'], {}, 'additive'),
    ('delivery_role.sql', ROLE_049['delivery_role.sql'], {}, 'manual'),  # GRANT EXECUTE ON FUNCTION
    ('gateway_role.sql', ROLE_050['gateway_role.sql'], HEARTBEATS, 'additive'),
    ('subscription_role.sql', ROLE_050['subscription_role.sql'], HEARTBEATS, 'additive'),
    ('delivery_role.sql', ROLE_050['delivery_role.sql'], HEARTBEATS, 'additive'),
], ids=['049-gateway', '049-subscription', '049-delivery', '050-gateway', '050-subscription', '050-delivery'])
def test_the_appended_role_text_of_049_and_050_classifies_as_merged(name, block, new_tables, expected):
    base = role_base(name, block)
    assert rollout.role_class(base, base + block, new_tables) == expected
    # The policy template is additive only on a table the same release creates.
    if new_tables: assert rollout.role_class(base, base + block, {}) == 'manual'


GATEWAY = 'gateway_role.sql'
GATEWAY_BASE = role_base(GATEWAY, ROLE_050[GATEWAY])
GATEWAY_POLICY = ROLE_050[GATEWAY].split('\n', 2)[2]
# Appended text the role language calls manual; each is valid SQL a careless edit could add.
ROLE_MANUAL = {
    'revoke': 'REVOKE SELECT ON messages FROM messaging_gateway;',
    'new-role': 'GRANT SELECT ON messages TO messaging_synthetic;',
    'membership': 'GRANT messaging_subscriptions TO messaging_gateway;',
    'alter-role': 'ALTER ROLE messaging_gateway BYPASSRLS;',
    'restrictive': GATEWAY_POLICY.replace('ON component_heartbeats TO', 'ON component_heartbeats AS RESTRICTIVE TO'),
    'permissive-spelled': GATEWAY_POLICY.replace('ON component_heartbeats TO', 'ON component_heartbeats AS PERMISSIVE TO'),
    'policy-other-table': GATEWAY_POLICY.replace('component_heartbeats', 'messages'),
    'policy-for-all': GATEWAY_POLICY.replace(' TO messaging_gateway USING', ' FOR ALL TO messaging_gateway USING'),
    'policy-other-role': GATEWAY_POLICY.replace('TO messaging_gateway', 'TO messaging_synthetic'),
    'policy-expression': GATEWAY_POLICY.replace("USING (component='api')", 'USING (true)'),
    # Astra r1 F1: value words in the column slots make an always-true policy.
    'policy-value-word': GATEWAY_POLICY.replace("(component='api')", "(true='true')"),
    'policy-current-user': GATEWAY_POLICY.replace("(component='api')", "(current_user='api')"),
    'policy-undeclared-column': GATEWAY_POLICY.replace("(component='api')", "(beat='api')"),
    # Astra r1 F3: PostgreSQL would truncate this name to an existing table's.
    'policy-long-name': GATEWAY_POLICY.replace('messaging_gateway_beat', 'b' * 64),
    'other-do': "DO $$ BEGIN EXECUTE 'GRANT ALL ON messages TO messaging_gateway'; END $$;",
    'truncate': 'GRANT TRUNCATE ON messages TO messaging_gateway;',
    'truncate-statement': 'TRUNCATE messages;',
    'all': 'GRANT ALL ON messages TO messaging_gateway;',
    'all-privileges': 'GRANT ALL PRIVILEGES ON messages TO messaging_gateway;',
    'trigger': 'GRANT TRIGGER ON messages TO messaging_gateway;',
    'grant-option': 'GRANT SELECT ON messages TO messaging_gateway WITH GRANT OPTION;',
    'schema': 'GRANT CREATE ON SCHEMA public TO messaging_gateway;',
    'database': 'GRANT CONNECT ON DATABASE messaging TO messaging_gateway;',
    'function': 'GRANT EXECUTE ON FUNCTION public.synthetic() TO messaging_gateway;',
    'sequence': 'GRANT USAGE ON SEQUENCE synthetic_seq TO messaging_gateway;',
    'all-tables': 'GRANT SELECT ON ALL TABLES IN SCHEMA public TO messaging_gateway;',
    'public': 'GRANT SELECT ON messages TO PUBLIC;',
    'two-roles': 'GRANT SELECT ON messages TO messaging_gateway, messaging_subscriptions;',
    'granted-by': 'GRANT SELECT ON messages TO messaging_gateway GRANTED BY CURRENT_USER;',
    'other-schema': 'GRANT SELECT ON pg_catalog.pg_authid TO messaging_gateway;',
    'set-role': 'SET ROLE messaging_gateway;',
    'untokenizable': "GRANT SELECT ON messages TO messaging_gateway; -- ok\nSELECT U&'x';",
}


# spec: ANYM-596
@pytest.mark.parametrize('example', ROLE_MANUAL)
def test_role_text_outside_the_role_language_is_manual(example):
    assert rollout.role_class(GATEWAY_BASE, GATEWAY_BASE + ROLE_MANUAL[example] + '\n', HEARTBEATS) == 'manual'


# spec: ANYM-596
def test_a_role_file_whose_base_text_changed_is_manual():
    candidate = GATEWAY_BASE + ROLE_050[GATEWAY]
    assert rollout.role_class(GATEWAY_BASE, candidate, HEARTBEATS) == 'additive'
    assert rollout.role_class(GATEWAY_BASE, GATEWAY_BASE, {}) == 'additive'  # nothing appended
    edited = candidate.replace('GRANT USAGE ON SCHEMA public', 'GRANT CREATE ON SCHEMA public', 1)
    assert edited != candidate and rollout.role_class(GATEWAY_BASE, edited, HEARTBEATS) == 'manual'
    assert rollout.role_class(GATEWAY_BASE, GATEWAY_BASE[:-1], {}) == 'manual'  # shortened
    # The appended text starts a statement: a base ending inside a statement cannot be continued.
    open_base = GATEWAY_BASE + 'GRANT SELECT ON messages'
    assert rollout.role_class(open_base, open_base + ' TO messaging_gateway;\n', {}) == 'manual'


# spec: ANYM-596
def test_with_the_table_inventory_a_grant_on_anything_but_a_known_table_is_manual():
    # Astra r1 F2: PostgreSQL takes `GRANT UPDATE ON <sequence>` without the SEQUENCE keyword.
    implicit = GATEWAY_BASE + 'GRANT UPDATE ON synthetic_seq TO messaging_gateway;\n'
    assert rollout.role_class(GATEWAY_BASE, implicit, {}, tables=EXISTING) == 'manual'
    known = GATEWAY_BASE + 'GRANT SELECT ON messages, public.component_heartbeats TO messaging_gateway;\n'
    assert rollout.role_class(GATEWAY_BASE, known, HEARTBEATS, tables=EXISTING) == 'additive'
    assert rollout.role_class(GATEWAY_BASE, known, {}, tables=EXISTING) == 'manual'
    long = GATEWAY_BASE + f'GRANT SELECT ON {"m" * 64} TO messaging_gateway;\n'
    assert rollout.role_class(GATEWAY_BASE, long, {}, tables={'m' * 64}) == 'manual'


# spec: ANYM-596
def test_a_name_postgresql_would_truncate_is_never_unattended():
    existing = 't' * 63
    text = EXPAND + f'CREATE TABLE IF NOT EXISTS {existing}x (component text);\n'
    assert rollout.rollout_class(text, {existing}) == 'expand'  # the CI class is unchanged
    assert rollout.unattended_class(text, {existing}) == 'manual'
    assert rollout.unattended_class(text.replace('x (', ' ('), set()) == 'expand'


ADDITIVE_STATEMENTS = [ROLE_049['gateway_role.sql'], ROLE_050['gateway_role.sql'], GATEWAY_POLICY,
                       '-- a comment only\n', 'GRANT REFERENCES ON messages TO messaging_gateway;\n',
                       'GRANT DELETE ON TABLE public.messages, chats TO messaging_gateway;\n']
MANUAL_STATEMENTS = [ROLE_MANUAL[k] + '\n' for k in ROLE_MANUAL]


# spec: ANYM-596
@given(st.lists(st.sampled_from(ADDITIVE_STATEMENTS), max_size=6),
       st.lists(st.sampled_from(ADDITIVE_STATEMENTS), max_size=6), st.sampled_from(MANUAL_STATEMENTS))
@settings(max_examples=200, deadline=None)
def test_appended_additive_role_statements_stay_additive_until_one_manual_statement_joins(before, after, manual):
    additive = ''.join(before + after)
    assert rollout.role_class(GATEWAY_BASE, GATEWAY_BASE + additive, HEARTBEATS) == 'additive'
    assert rollout.role_class(GATEWAY_BASE, GATEWAY_BASE + ''.join(before) + manual + ''.join(after),
                              HEARTBEATS) == 'manual'


# spec: ANYM-596
def test_a_policy_may_use_a_column_a_later_add_column_gives_a_new_table():
    # Sol r2 F3: the column map follows ADD COLUMN on a new table, in the same file and in a later one.
    first = 'CREATE TABLE synthetic_beats (id text PRIMARY KEY);\nALTER TABLE synthetic_beats ADD COLUMN component text;\n'
    created = rollout.new_tables(first)
    assert created == {'synthetic_beats': {'id', 'component'}}
    later = rollout.new_tables('ALTER TABLE synthetic_beats ADD COLUMN state text;\n', created)
    assert later == {'synthetic_beats': {'id', 'component', 'state'}} and created['synthetic_beats'] == {'id', 'component'}
    policy = GATEWAY_POLICY.replace('component_heartbeats', 'synthetic_beats')
    assert rollout.role_class(GATEWAY_BASE, GATEWAY_BASE + policy, created) == 'additive'
    stated = policy.replace("(component='api')", "(state='api')")
    assert rollout.role_class(GATEWAY_BASE, GATEWAY_BASE + stated, created) == 'manual'
    assert rollout.role_class(GATEWAY_BASE, GATEWAY_BASE + stated, later) == 'additive'


# spec: ANYM-596
def test_a_quoted_serial_type_is_manual_like_an_unquoted_one():
    # Sol r3: PostgreSQL reads "serial" as the pseudo-type too and creates a sequence beside the table.
    for column in ('id serial', 'id "serial"', 'id "BIGSERIAL"'.lower()):
        text = EXPAND + f'CREATE TABLE synthetic_owner ({column});\n'
        assert rollout.classify(text) == 'manual'
        assert rollout.unattended_class(text, set()) == 'manual'
    assert rollout.classify(EXPAND + 'CREATE TABLE synthetic_owner (id "text");\n') == 'expand'
