# Contributing

Thanks for your interest in DeedKit. Bug reports, documentation fixes and pull
requests are welcome. The project is MIT licensed, and contributions are accepted
under that license.

## Propose a change

- Small fixes (typos, docs, an obvious bug with a test): open a pull request.
- Anything larger (a new capability, a schema or API change, a new provider
  behavior): open an issue first and describe the problem and the approach, so
  nobody builds something that cannot be merged.
- Security problems: do not open a public issue. Follow [SECURITY.md](SECURITY.md).

Read the README and the document of the module you change. Behavior is specified
in the module documents (SENDING.md, ACCESS.md, SEARCH.md and similar); a change
of meaning updates them in the same pull request.

## Set up and check locally

Use Python 3.13, PostgreSQL 18 and Node 24. All fixtures must be synthetic. Never
commit runtime secrets, sessions, database dumps, recordings or personal data.

```sh
uv sync --group dev
# Point only at an isolated database named messaging_probe_test:
PROBE_TEST_DSN_FILE=/private/test.dsn uv run pytest tests
node --test whatsapp/tests/*.test.mjs
npm test --prefix packages/npm
python scripts/build.py
npm pack --prefix packages/npm --dry-run
```

Python tests deliberately fail if the database fixture is used without an isolated
test DSN. Some OCR tests need `pdftotext` and Tesseract; audio tests explicitly state
when native prerequisites are absent. CI installs those tools. Never use production
credentials in tests. The services under `services/` and `whatsapp/whatsmeow` have
their own test commands; the full list CI runs is in
[`checks.yml`](.github/workflows/checks.yml) and
[`whatsmeow.yml`](.github/workflows/whatsmeow.yml).

Keep applied SQL migrations unchanged. Add a new migration for schema changes,
update backup table inventory/restore ceilings and check roles/ACL and repeat migration.
Every messaging migration numbered 041 or later starts with `-- rollout: expand` or
`-- rollout: manual` (ANYM-444; 001-040 keep no header and count as manual). `expand`
means old code keeps working, and `classify` in
`services/messaging/messaging_service/migration_rollout.py` allows it only when every
statement is one of: `SET LOCAL lock_timeout|statement_timeout` to `'<n>ms'`/`'<n>s'`
(above 0, at most 5 s and 180 s), or `lock_timeout` to `DEFAULT`; `CREATE TABLE` of a new table
(defaults: a literal, `now()`, `gen_random_uuid()`; column constraints, references and
`CHECK`/`PRIMARY KEY`/`UNIQUE`/`FOREIGN KEY` table constraints; a reference names, unqualified or
`public.`, a table that exists or that the file or an earlier file of the range creates); `CREATE [UNIQUE] INDEX [name] ON
<table the file creates> [USING btree] (<column> [ASC|DESC], ...)`; `ALTER TABLE ... ADD COLUMN <name> <type>
[NULL] [DEFAULT <literal>]` (`NOT NULL` only with a literal default, no constraint, generated
or identity column); a `DROP CONSTRAINT c` directly followed by `ADD CONSTRAINT c CHECK (<col>
IN (<literals>)) NOT VALID` or `CHECK (<col> <@ ARRAY[<literals>]::text[]) NOT VALID`; 036's
`DO` precondition template; `COMMENT ON TABLE <t>` or `COMMENT ON COLUMN <t>.<c> IS '<literal>'` of such
a table (ANYM-619: a comment on a schema, role, function or any other object is `manual`). Anything else (transaction control,
DML, functions, triggers, other `DO` blocks, grants, drops, renames, type/default/nullability
changes), and any text not lexed exactly as PostgreSQL does (U&, B, X or N strings, a string
continued after a newline, an unclosed quote or comment), is `manual`.
`tests/test_migration_rollout.py` never executes a declared `expand` that `classify` rejects; it
applies the others to PostgreSQL and fails unless the catalog only gained those kinds of change
(row security included; a widened list must be a superset of numbers or strings). Nothing runs a migration because of its class: `deploy` only names it in its pause.
The unattended entry point `python -m messaging_service migrate-expand --plan <file>` (ANYM-596; `migrate`
is unchanged) runs only files whose `unattended_class` is `expand`: a `rollout_class` `expand` with no
DROP of any kind, so the widening pair stays manual. Role files may only grow: `role_class` calls the
appended text `additive` when the file still starts with its old text byte for byte and every appended
statement is a comment, a `GRANT <SELECT|INSERT|UPDATE|DELETE|REFERENCES>[(<columns>)], ... ON [TABLE]
<table>, ... TO <role>` to a role the old text already grants to, or 050's permissive `DO ... CREATE POLICY`
template on a table the same release creates; REVOKE, ALL, TRUNCATE, TRIGGER, `WITH GRANT OPTION`,
schema/function/sequence grants, role membership, `ALTER ROLE` and other `DO` blocks are manual. It runs
under `lock_timeout` 2 s, `statement_timeout` 4 s and `transaction_timeout` 4 s (read back first), takes
the migration advisory lock without waiting (`busy`), checks the ledger against the image, and applies the
files, their ledger rows and the appended role text in one transaction; it never runs `schema.sql`. Its
output is one JSON line with `schema_version` or a fixed `error` code.
Keep source revisions, original provider IDs and acknowledgement semantics stable.
Do not rename or move existing tests.

## Sign off your commits (DCO)

DeedKit uses the [Developer Certificate of Origin](https://developercertificate.org)
(DCO 1.1) instead of a contributor license agreement. There is nothing to sign or
register: by adding a `Signed-off-by` line you certify that you wrote the change or
have the right to submit it under the project's license.

```sh
git commit -s -m "fix: describe the change"
```

This appends `Signed-off-by: Your Name <you@example.com>`, using your `user.name`
and `user.email`. The name and email must match the commit author.

Forgot? Fix it before the pull request is merged:

```sh
git commit --amend -s --no-edit        # the last commit
git rebase --signoff main              # every commit of the branch
git push --force-with-lease
```

## What happens to your pull request

1. CI runs the same checks as for any change. For a pull request from a fork it
   runs without repository secrets and publishes nothing to the project's Tiden
   workspace (see [TIDEN-CI.md](TIDEN-CI.md)). GitHub may ask a maintainer to
   approve the first run.
2. The DCO check verifies the sign-off on every commit.
3. A maintainer (currently the project owner) reviews the change and may ask for edits.
4. The same maintainer merges once the required checks and review are green.

Maintainers handle the project's internal tracking; you do not need a Tiden account.
