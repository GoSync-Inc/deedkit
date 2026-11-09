-- rollout: expand
-- ANYM-816: an operator recovery of failed WhatsApp originals keeps its own durable state (media_recovery.py).
-- A job's error_code (ANYM-815's fixed cause) and the eight-attempt cap (ANYM-699) are not used for it: each
-- recovered job spends a budget of 3 attempts counted here. Additive: a schema-67 runtime never reads the table and
-- never claims a recovery job: while a recovery holds a job, media_jobs.lease_until is a far-future hold
-- (media_recovery.LEASE_HOLD) that the schema-67 claim and lease-expiry sweep never take, and the real expiry is
-- media_recovery.lease_until. Rows go with their job (ON DELETE CASCADE, as
-- media_reservations). A bound collector role updates only its account's rows: the CHECK calls 037's binding
-- function for the current role and the login, as conflict_reevaluations (057) does. Grants: collector_role.sql
-- and gateway_role.sql, appended; the operator command runs as the owner.
-- Risk tier: non-critical (a new table and its index; no existing row is deleted or rewritten).
-- Repeatable (IF NOT EXISTS, as 058): a migrate() that replays it finds the table and index in place.
SET LOCAL lock_timeout='2s';
CREATE TABLE IF NOT EXISTS media_recovery (
 job text PRIMARY KEY REFERENCES media_jobs(id) ON DELETE CASCADE,
 account text NOT NULL REFERENCES accounts(id),
 run_id text NOT NULL CHECK(run_id ~ '^[0-9a-f]{32}$'),
 attempts smallint NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 3),
 next_due timestamptz NOT NULL DEFAULT now(),
 lease_until timestamptz,
 state text NOT NULL CHECK(state IN ('queued','running','recovered','provider_unavailable','still_failing','refused')),
 refusal_reason text CHECK(refusal_reason ~ '^[a-z_]{1,64}$'),
 last_code text CHECK(last_code ~ '^[a-z0-9_]{1,64}$'),
 created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 CHECK((state='refused')=(refusal_reason IS NOT NULL)),
 CHECK(account=coalesce(public.collector_bound_account(current_user::text),account)
  AND account=coalesce(public.collector_bound_account(session_user::text),account))
);
CREATE INDEX IF NOT EXISTS media_recovery_due ON media_recovery(account,state,next_due);
