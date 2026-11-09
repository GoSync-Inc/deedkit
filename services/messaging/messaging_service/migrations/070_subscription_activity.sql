-- rollout: expand
-- Risk tier: non-critical (one new table; grants only on it; no existing row, column, constraint or grant changes).
-- ANYM-824: per UTC day, platform, scope version and metric, how many events a subscription's matcher scanned,
-- matched and left out (and why). Numbers only: no chat, sender, message or account ID and no text. The matcher adds a
-- pass's counts in the same transaction that advances matched_through (one upsert per definition and pass, in a
-- savepoint); get_subscription shows the last 7 days of the key's current scope version; the retention job deletes
-- rows older than 35 days and a retired definition's rows. A schema-69 runtime ignores the table; a schema-70 runtime
-- on a schema-69 database skips the counts and answers get_subscription without `activity`. Grants: the worker role
-- reads and writes this table only (subscription_role.sql), the API role reads it and adds read counts, no DELETE (gateway_role.sql).
SET LOCAL lock_timeout='2s';
CREATE TABLE IF NOT EXISTS subscription_activity (
    subscription uuid NOT NULL REFERENCES subscriptions(id), day date NOT NULL, platform text NOT NULL,
    scope_version bigint NOT NULL, metric text NOT NULL, n bigint NOT NULL CHECK (n >= 0),
    PRIMARY KEY (subscription, day, platform, scope_version, metric)
);
