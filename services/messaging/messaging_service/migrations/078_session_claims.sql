-- rollout: expand
-- Risk tier: non-critical (one new table and grants only on it; no existing row, column or grant changes;
-- docs/operations/production.md "Migration risk tiers").
-- ANYM-914: one durable provider-session owner per account, bound to one lock domain (host_id, kernel boot_id and
-- the canonical host lock file's st_dev:st_ino). Within one boot the kernel flock proves a previous owner dead;
-- anything else needs a clean release or an operator release (session_claim.py, deedkit admin session-claim).
-- `restore_hold` is set only by quarantine_restore on an isolated restore. `waiting_*` show a refused opener and
-- never change ownership. A schema-77 runtime never names the table; a schema-78 opener on a schema-77 database
-- refuses at acquire (fail-closed at start). No column default calls a function: session_claim.py writes times.
SET LOCAL lock_timeout='2s';
CREATE TABLE IF NOT EXISTS session_claims (
 account text PRIMARY KEY REFERENCES accounts(id),
 host_id text NOT NULL CHECK(host_id ~ '^[0-9a-f]{32}$'),
 boot_id text NOT NULL CHECK(length(boot_id) BETWEEN 1 AND 64),
 lock_ref text NOT NULL CHECK(lock_ref ~ '^[0-9]{1,20}:[0-9]{1,20}$'),
 holder_id text NOT NULL CHECK(holder_id ~ '^[0-9a-f]{32}$'),
 holder_kind text NOT NULL CHECK(holder_kind IN ('collector','pair','holder')),
 generation bigint NOT NULL CHECK(generation>0),
 claimed_at timestamptz NOT NULL,
 seen_at timestamptz NOT NULL,
 released_at timestamptz,
 released_by text CHECK(released_by IN ('holder','admin')),
 restore_hold boolean NOT NULL DEFAULT false,
 waiting_host_id text CHECK(waiting_host_id ~ '^[0-9a-f]{32}$'),
 waiting_since timestamptz,
 CHECK((released_at IS NULL)=(released_by IS NULL))
);
