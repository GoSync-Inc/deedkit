"""Rollout class of messaging migrations (ANYM-444): a positive language checked before execution, a header rule,
and a real-PostgreSQL catalog check of every declared `expand`. Synthetic SQL only, in a disposable database."""
from pathlib import Path
import re
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row, tuple_row
import pytest

from messaging_service import migration_rollout as rollout
from messaging_service import store as store_module

SOURCE = Path(store_module.__file__).parent
MIGRATIONS = {int(path.name[:3]): path for path in SOURCE.joinpath('migrations').glob('[0-9][0-9][0-9]_*.sql')}
# The catalog database is built through 035, so 036 is checked against exactly its predecessors.
BASE = 35
SYNTHETIC = f'{rollout.HEADER_FROM:03d}_synthetic.sql'
# Pre-existing objects the synthetic files change: built after the predecessors, before the snapshot.
SETUP = """CREATE TABLE synthetic_existing (id text PRIMARY KEY,
 kind text CONSTRAINT synthetic_existing_kind_check CHECK(kind IN ('a','b')),
 tags text[] CONSTRAINT synthetic_existing_tags_check CHECK(tags <@ ARRAY['a','b']::text[]));
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
 seen_at timestamptz DEFAULT clock_timestamp(),
 CONSTRAINT synthetic_notes_unique UNIQUE(message,note),
 FOREIGN KEY(note) REFERENCES synthetic_existing(id)
);
""",
    'create-index': """CREATE TABLE synthetic_marks (name text, rank integer NOT NULL DEFAULT 0);
CREATE UNIQUE INDEX synthetic_marks_name ON synthetic_marks(name) WHERE name IS NOT NULL;
CREATE INDEX ON synthetic_marks USING btree (lower(name), rank);
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
}
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
            c.reloptions::text, c.relfilenode, t.relname FROM pg_class c {public}
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


LIST = re.compile(r"CHECK \(\((\w+) (= ANY \(ARRAY\[(.*)\]\)|<@ ARRAY\[(.*)\])\)\)( NOT VALID)?")


def values(definition):
    """(column, form, values) of `CHECK ((c = ANY (ARRAY['a'::text, …])))` or `CHECK ((c <@ ARRAY[…]))`, or None."""
    match = LIST.fullmatch(definition)
    if not match: return None
    items = match[3] if match[3] is not None else match[4]
    found = re.findall(r"'((?:[^']|'')*)'::text", items)
    return (match[1], match[3] is None, set(found)) if ', '.join(f"'{v}'::text" for v in found) == items else None


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
                c.execute(MIGRATIONS[version].read_text())
                if version == 17:
                    from messaging_service.chat_evidence import backfill
                    backfill(c)
            c.commit()
            yield c
    finally:
        with psycopg.connect(dsn, autocommit=True) as owner:
            owner.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))


def check(c, text, *, upto, setup=''):
    """`classify` of `text` with the real table inventory, and the catalog problems of applying it after the
    migrations through `upto` and `setup`."""
    try:
        for version in sorted(v for v in MIGRATIONS if BASE < v <= upto):
            c.execute(MIGRATIONS[version].read_text())
        if setup: c.execute(setup)
        before = snapshot(c)
        verdict = rollout.classify(text, tables(c))
        c.execute(text)
        return verdict, catalog_problems(before, snapshot(c))
    finally:
        c.rollback()


def rollout_failures(c, name, text, *, setup='', apply=True):
    """What fails CI for migration file `name`: the header rule and, for a declared expand, classify and the catalog."""
    if problem := rollout.header_problem(name, text): return [problem]
    if rollout.header(text) != 'expand': return []
    if not apply: return [] if rollout.classify(text) == 'expand' else [f'{name}: classify says manual']
    verdict, problems = check(c, text, upto=int(name[:3]) - 1, setup=setup)
    return ([f'{name}: classify says manual'] if verdict != 'expand' else []) + problems


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
    assert rollout_failures(catalog, SYNTHETIC, EXPAND + text, setup=SETUP, apply=catalog_rejects is not None)
    if catalog_rejects:
        # The catalog check rejects it on its own, independent of classify.
        assert check(catalog, text, upto=rollout.HEADER_FROM - 1, setup=SETUP)[1]


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
def test_036_classifies_expand_and_passes_the_catalog_check_after_035(catalog):
    text = MIGRATIONS[36].read_text()
    assert rollout.classify(text) == 'expand'
    assert check(catalog, text, upto=BASE) == ('expand', [])


# spec: ANYM-444
def test_applied_migrations_need_no_header_and_every_later_one_declares_its_class():
    assert rollout.HEADER_FROM == 41 and 40 in MIGRATIONS
    for version, path in MIGRATIONS.items():
        text = path.read_text()
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
        assert rollout_failures(catalog, path.name, path.read_text()) == []
    # The same check fails a file that breaks the rule or claims expand for a non-expand body.
    assert rollout_failures(catalog, SYNTHETIC, ALLOWED['add-column'])
    assert rollout_failures(catalog, SYNTHETIC, EXPAND + "INSERT INTO messages_synthetic VALUES (1);", apply=False)


# spec: ANYM-444
def test_the_pause_class_is_no_header_a_declared_manual_or_classify_of_a_declared_expand():
    body = ALLOWED['add-column']
    assert rollout.rollout_class(body) == 'no header'
    assert rollout.rollout_class('-- rollout: manual\n' + body) == 'manual'
    assert rollout.rollout_class(EXPAND + body) == 'expand'
    assert rollout.rollout_class(EXPAND + COUNTEREXAMPLES['insert'][0]) == 'manual'
