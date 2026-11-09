-- rollout: manual
-- Risk tier: non-critical (three new tables with row-level security and a policy on each; no existing row, column
-- or grant changes; grants only on these tables in the role files; docs/operations/production.md "Migration risk
-- tiers"). The declared header is manual because the expand header's `classify` names no row-level security or
-- policy statement; migrate-expand's `unattended_class` calls this file `expand` and the role files only append
-- grants, so unattended deploy releases it ("Unattended migrations"), no backup needed.
-- ANYM-921 (the tables of ANYM-920): a poll message's current results and who chose which option.
-- poll_results: one row per option of the poll with its voter count. poll_voters: one row per voter and chosen
-- option. poll_votes: the newest vote time of each voter, kept for a retracted (empty) vote too, so an older vote
-- delivered late never wins. Voter IDs are the archive's sender IDs; no message text. A schema-82 runtime never
-- names these tables; purge (message_purge.py) deletes a purged poll's rows. A bound collector (037's binding
-- function, for the current role and the login, as 057's CHECK) sees, updates and deletes only its own account's rows
-- and writes a row only for a message of that account: row-level security on each new table, one policy for every
-- role (USING: the old row, WITH CHECK: the new row). An unbound role (gateway, purge, a shared collector) passes the
-- policy unchanged; reads also keep a row only when its account is the poll message's account.
SET LOCAL lock_timeout='2s';
CREATE TABLE IF NOT EXISTS poll_results (
 account text NOT NULL REFERENCES accounts(id),
 message text NOT NULL REFERENCES messages(id),
 option_index smallint NOT NULL CHECK(option_index BETWEEN 0 AND 11),
 voters integer NOT NULL CHECK(voters>=0),
 updated_at timestamptz NOT NULL,
 PRIMARY KEY(message,option_index),
 CHECK(account=coalesce(public.collector_bound_account(current_user::text),account)
  AND account=coalesce(public.collector_bound_account(session_user::text),account))
);
CREATE TABLE IF NOT EXISTS poll_voters (
 account text NOT NULL REFERENCES accounts(id),
 message text NOT NULL REFERENCES messages(id),
 option_index smallint NOT NULL CHECK(option_index BETWEEN 0 AND 11),
 voter text NOT NULL CHECK(length(voter) BETWEEN 1 AND 160),
 from_me boolean NOT NULL,
 updated_at timestamptz NOT NULL,
 PRIMARY KEY(message,voter,option_index),
 CHECK(account=coalesce(public.collector_bound_account(current_user::text),account)
  AND account=coalesce(public.collector_bound_account(session_user::text),account))
);
CREATE TABLE IF NOT EXISTS poll_votes (
 account text NOT NULL REFERENCES accounts(id),
 message text NOT NULL REFERENCES messages(id),
 voter text NOT NULL CHECK(length(voter) BETWEEN 1 AND 160),
 voted_at timestamptz NOT NULL,
 PRIMARY KEY(message,voter),
 CHECK(account=coalesce(public.collector_bound_account(current_user::text),account)
  AND account=coalesce(public.collector_bound_account(session_user::text),account))
);
ALTER TABLE poll_results ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS poll_results_account ON poll_results;
CREATE POLICY poll_results_account ON poll_results
 USING (account=coalesce(public.collector_bound_account(current_user::text),account)
  AND account=coalesce(public.collector_bound_account(session_user::text),account))
 WITH CHECK (account=coalesce(public.collector_bound_account(current_user::text),account)
  AND account=coalesce(public.collector_bound_account(session_user::text),account)
  AND EXISTS(SELECT FROM public.messages m WHERE m.id=poll_results.message AND m.account=poll_results.account));
ALTER TABLE poll_voters ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS poll_voters_account ON poll_voters;
CREATE POLICY poll_voters_account ON poll_voters
 USING (account=coalesce(public.collector_bound_account(current_user::text),account)
  AND account=coalesce(public.collector_bound_account(session_user::text),account))
 WITH CHECK (account=coalesce(public.collector_bound_account(current_user::text),account)
  AND account=coalesce(public.collector_bound_account(session_user::text),account)
  AND EXISTS(SELECT FROM public.messages m WHERE m.id=poll_voters.message AND m.account=poll_voters.account));
ALTER TABLE poll_votes ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS poll_votes_account ON poll_votes;
CREATE POLICY poll_votes_account ON poll_votes
 USING (account=coalesce(public.collector_bound_account(current_user::text),account)
  AND account=coalesce(public.collector_bound_account(session_user::text),account))
 WITH CHECK (account=coalesce(public.collector_bound_account(current_user::text),account)
  AND account=coalesce(public.collector_bound_account(session_user::text),account)
  AND EXISTS(SELECT FROM public.messages m WHERE m.id=poll_votes.message AND m.account=poll_votes.account));
