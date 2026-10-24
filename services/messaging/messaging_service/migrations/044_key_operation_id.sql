-- rollout: manual
-- Repeatable key issue (ANYM-495). Expand-only: two nullable columns and a partial unique index;
-- deployed schema-43 code never reads or writes them. operation_request is the sha256 of the
-- issue request, so a replay of the same operation ID with another request is refused.
-- The ALTER holds an exclusive lock on api_keys (read by every authentication) until commit.
-- An authentication waits at most the runtime's 5 s statement timeout. The queued wait for the
-- lock (lock_timeout 2 s) and the two steps that hold it (1 s each; the check and the index scan
-- one row per key) are bounded one by one; transaction_timeout bounds the whole hold from here to
-- commit at 4 s, the rest of migrate() included: past it the server ends the session and rolls
-- back, so a slow upgrade fails and is retried instead of failing logins. migrate() sets no
-- transaction_timeout, so the timer starts at this SET; it is not reset, since DEFAULT stops it.
SET LOCAL transaction_timeout='4s';
SET LOCAL lock_timeout='2s';
SET LOCAL statement_timeout='1s';
ALTER TABLE api_keys ADD COLUMN operation_id text, ADD COLUMN operation_request text,
 ADD CONSTRAINT api_keys_operation_check CHECK((operation_id IS NULL)=(operation_request IS NULL)
  AND (operation_id IS NULL OR operation_id ~ '^[A-Za-z0-9._/-]{1,128}$')
  AND (operation_request IS NULL OR operation_request ~ '^[0-9a-f]{64}$'));
CREATE UNIQUE INDEX api_keys_operation_id_key ON api_keys(operation_id) WHERE operation_id IS NOT NULL;
SET LOCAL statement_timeout=DEFAULT;
SET LOCAL lock_timeout=DEFAULT;
