-- rollout: manual
-- Risk tier: non-critical (one new table with row-level security and a policy, one index on it; no existing row,
-- column or grant changes; grants only on this table in the role files; docs/operations/production.md "Migration risk
-- tiers"). Declared manual as 083 is (the expand header's `classify` names no row-level security or policy
-- statement); migrate-expand's `unattended_class` calls this file `expand` and the role files only append grants, so
-- unattended deploy releases it ("Unattended migrations"), no backup needed.
-- ANYM-920: a Telegram poll message's provider state beside 083's poll_results and poll_voters. Telegram pushes a
-- poll's counts by its poll ID and names options by opaque bytes, so each archived poll keeps the poll ID (indexed),
-- the option keys in option order (hex), Telegram's own total_voters, the owner's chosen options, the observation
-- time of the stored counts (an older observation never wins), when the public voter list was last read, whether
-- that read was complete (Telegram returned every vote within the 200 cap), and the display names Telegram returned
-- with that list (bounded member-set text, no message text). A schema-83 runtime
-- never names this table; purge (message_purge.py) deletes a purged poll's row. A bound collector sees, updates and
-- writes only its own account's rows, for a message of that account, as 083.
SET LOCAL lock_timeout='2s';
CREATE TABLE IF NOT EXISTS telegram_polls (
 account text NOT NULL REFERENCES accounts(id),
 message text PRIMARY KEY REFERENCES messages(id),
 provider_poll_id text NOT NULL CHECK(provider_poll_id ~ '^[0-9]{1,20}$'),
 option_keys text[] NOT NULL CHECK(cardinality(option_keys)<=12),
 public_voters boolean NOT NULL,
 total_voters integer CHECK(total_voters>=0),
 chosen smallint[] NOT NULL DEFAULT '{}' CHECK(cardinality(chosen)<=12),
 results_at timestamptz NOT NULL,
 votes_read_at timestamptz,
 voters_complete boolean NOT NULL DEFAULT false,
 voter_names jsonb NOT NULL DEFAULT '{}' CHECK(jsonb_typeof(voter_names)='object'),
 CHECK(account=coalesce(public.collector_bound_account(current_user::text),account)
  AND account=coalesce(public.collector_bound_account(session_user::text),account))
);
CREATE INDEX IF NOT EXISTS telegram_polls_poll ON telegram_polls(account,provider_poll_id);
ALTER TABLE telegram_polls ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS telegram_polls_account ON telegram_polls;
CREATE POLICY telegram_polls_account ON telegram_polls
 USING (account=coalesce(public.collector_bound_account(current_user::text),account)
  AND account=coalesce(public.collector_bound_account(session_user::text),account))
 WITH CHECK (account=coalesce(public.collector_bound_account(current_user::text),account)
  AND account=coalesce(public.collector_bound_account(session_user::text),account)
  AND EXISTS(SELECT FROM public.messages m WHERE m.id=telegram_polls.message AND m.account=telegram_polls.account));
