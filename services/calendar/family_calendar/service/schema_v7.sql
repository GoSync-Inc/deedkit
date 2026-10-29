-- rollout: expand
-- Series reads (ANYM-614). Additive only: version-6 code never reads this table.
-- A calendar_schedule preview of a series time move asks the worker (the only process with provider secrets) for one
-- read of the series: the master and its exceptions, which the preview lists with their fates. The gateway inserts a
-- request, the worker answers it in `result` (event fields as the occurrence cache keeps them) at `answered_at`, from
-- which a repeat preview reuses it for a few seconds. No foreign key: the migration takes no lock on principals or
-- calendars (as migrations 3-6).
CREATE TABLE series_reads(id text PRIMARY KEY, principal_id text NOT NULL, calendar_ref text NOT NULL,
 target text NOT NULL, state text NOT NULL DEFAULT 'prepared', result jsonb, error text,
 created_at timestamptz NOT NULL DEFAULT now(), answered_at timestamptz);
CREATE INDEX series_reads_pending ON series_reads(state, created_at);
