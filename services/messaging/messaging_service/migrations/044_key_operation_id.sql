-- rollout: expand
-- Repeatable key issue (ANYM-495). One row per key issued with an operation ID; operation_request is
-- the sha256 of the issue request, so a replay of the same operation ID with another request is
-- refused. A new table without a foreign key: applying it takes no lock on api_keys, which every
-- authentication reads, and deployed schema-43 code never reads it. Written and read only through the
-- owner connection (operator CLI), so it has no grant.
CREATE TABLE api_key_operations (
 operation_id text PRIMARY KEY CHECK(operation_id ~ '^[A-Za-z0-9._/-]{1,128}$'),
 operation_request text NOT NULL CHECK(operation_request ~ '^[0-9a-f]{64}$'),
 key_id uuid NOT NULL UNIQUE
);
