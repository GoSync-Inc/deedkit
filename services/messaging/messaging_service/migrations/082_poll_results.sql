-- rollout: expand
-- Risk tier: non-critical (three new tables; no existing row, column or grant changes; grants only on these tables
-- in the role files; docs/operations/production.md "Migration risk tiers").
-- ANYM-921 (the tables of ANYM-920): a poll message's current results and who chose which option.
-- poll_results: one row per option of the poll with its voter count. poll_voters: one row per voter and chosen
-- option. poll_votes: the newest vote time of each voter, kept for a retracted (empty) vote too, so an older vote
-- delivered late never wins. Voter IDs are the archive's sender IDs; no message text. A schema-81 runtime never
-- names these tables; purge (message_purge.py) deletes a purged poll's rows. A bound collector writes only its own
-- account's rows (the CHECK calls 037's binding function, as 057 does); reads keep a row only when its account is
-- the poll message's account.
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
