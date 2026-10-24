-- Repeatable key issue (ANYM-495). Expand-only: two nullable columns and a partial unique index;
-- deployed schema-43 code never reads or writes them. operation_request is the sha256 of the
-- issue request, so a replay of the same operation ID with another request is refused.
-- A short lock wait fails the upgrade instead of stalling authentication on api_keys.
SET LOCAL lock_timeout='2s';
ALTER TABLE api_keys ADD COLUMN operation_id text, ADD COLUMN operation_request text,
 ADD CONSTRAINT api_keys_operation_check CHECK((operation_id IS NULL)=(operation_request IS NULL)
  AND (operation_id IS NULL OR operation_id ~ '^[A-Za-z0-9._/-]{1,128}$')
  AND (operation_request IS NULL OR operation_request ~ '^[0-9a-f]{64}$'));
CREATE UNIQUE INDEX api_keys_operation_id_key ON api_keys(operation_id) WHERE operation_id IS NOT NULL;
SET LOCAL lock_timeout=DEFAULT;
