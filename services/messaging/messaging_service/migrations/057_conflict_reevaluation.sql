-- rollout: expand
-- Conflict re-evaluation (ANYM-621): each collector re-checks its account's open version conflicts under each new
-- rule by itself (conflict_reevaluation.py). Additive: N-1 code resolves without `resolution` (NULL reads as
-- unknown) and never reads the new table. A bound collector role writes only its account's progress rows: the CHECK
-- calls 037's binding function for the current role and the login, as collector_account_guard does, and is NULL
-- (unbound) for operators and restores.
SET LOCAL lock_timeout='2s';
ALTER TABLE message_conflicts ADD COLUMN resolution text;
CREATE TABLE conflict_reevaluations (
 job text NOT NULL CHECK(job ~ '^[a-z][a-z0-9_]{0,47}$'),
 job_version smallint NOT NULL CHECK(job_version>0),
 account text NOT NULL REFERENCES accounts(id),
 cursor text,
 watermark timestamptz,
 done_at timestamptz,
 last_error text CHECK(last_error ~ '^[a-z_]{1,64}$'),
 PRIMARY KEY(job,job_version,account),
 CHECK((cursor IS NULL)=(watermark IS NULL)),
 CHECK(account=coalesce(public.collector_bound_account(current_user::text),account)
  AND account=coalesce(public.collector_bound_account(session_user::text),account))
);
