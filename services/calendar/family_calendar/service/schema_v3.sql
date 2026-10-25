-- Repeatable agent-key issue (ANYM-495). One row per principal issued with an operation ID;
-- operation_request is the sha256 of the issue request, so a replay of the same operation ID with another
-- request is refused. A new table without a foreign key: applying it takes no lock on principals, which every
-- request reads, and version-2 code never reads it.
CREATE TABLE principal_operations (
 operation_id text PRIMARY KEY CHECK(operation_id ~ '^[A-Za-z0-9._/-]{1,128}$'),
 operation_request text NOT NULL CHECK(operation_request ~ '^[0-9a-f]{64}$'),
 principal_id text NOT NULL UNIQUE
);
