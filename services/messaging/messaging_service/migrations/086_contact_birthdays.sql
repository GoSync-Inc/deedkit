-- rollout: manual
-- Risk tier: non-critical (one new table with row-level security and a policy; no existing row, column or grant
-- changes; grants only on this table in the role files; docs/operations/production.md "Migration risk tiers").
-- Declared manual as 084 is (the expand header's `classify` names no row-level security or policy statement);
-- migrate-expand's `unattended_class` calls this file `expand`, so unattended deploy releases it.
-- ANYM-919: the Telegram birthday (day, month, optional year) of each active contact of an account
-- (birthdays.py: the private chat holds at least 3 outgoing and 1 incoming message in the last 90 days), or
-- `hidden` when the user does not share it. The collector's birthday pass alone writes it and deletes the row of a
-- user who is no longer active at its next pass (owner 2026-11-09: only contacts the owner actively talks to; no
-- calendar). Nothing else references it; a schema-85 runtime never names it. A bound collector sees, updates,
-- writes and deletes only its own account's rows, as 084.
SET LOCAL lock_timeout='2s';
CREATE TABLE IF NOT EXISTS contact_birthdays (
 account text NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
 provider_user text NOT NULL CHECK(provider_user ~ '^[1-9][0-9]{0,19}$'),
 day smallint CHECK(day BETWEEN 1 AND 31),
 month smallint CHECK(month BETWEEN 1 AND 12),
 year smallint CHECK(year BETWEEN 1 AND 9999),
 state text NOT NULL CHECK(state IN ('known','hidden')),
 checked_at timestamptz NOT NULL,
 PRIMARY KEY(account,provider_user),
 CHECK(state='known' AND day IS NOT NULL AND month IS NOT NULL
  OR state='hidden' AND day IS NULL AND month IS NULL AND year IS NULL),
 CHECK(account=coalesce(public.collector_bound_account(current_user::text),account)
  AND account=coalesce(public.collector_bound_account(session_user::text),account))
);
ALTER TABLE contact_birthdays ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS contact_birthdays_account ON contact_birthdays;
CREATE POLICY contact_birthdays_account ON contact_birthdays
 USING (account=coalesce(public.collector_bound_account(current_user::text),account)
  AND account=coalesce(public.collector_bound_account(session_user::text),account))
 WITH CHECK (account=coalesce(public.collector_bound_account(current_user::text),account)
  AND account=coalesce(public.collector_bound_account(session_user::text),account));
