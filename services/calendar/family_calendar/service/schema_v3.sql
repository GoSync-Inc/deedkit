-- Repeatable agent-key issue (ANYM-495). Expand-only: two nullable columns and a partial unique index;
-- deployed version-2 code never reads or writes them. operation_request is the sha256 of the issue request,
-- so a replay of the same operation ID with another request is refused.
SET LOCAL lock_timeout = '2s';
ALTER TABLE principals ADD COLUMN operation_id text, ADD COLUMN operation_request text,
 ADD CONSTRAINT principals_operation_check CHECK((operation_id IS NULL) = (operation_request IS NULL)
  AND (operation_id IS NULL OR operation_id ~ '^[A-Za-z0-9._/-]{1,128}$')
  AND (operation_request IS NULL OR operation_request ~ '^[0-9a-f]{64}$'));
CREATE UNIQUE INDEX principals_operation_id_key ON principals(operation_id) WHERE operation_id IS NOT NULL;
SET LOCAL lock_timeout = DEFAULT;
